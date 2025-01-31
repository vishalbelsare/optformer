# Copyright 2024 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Transformer model for ICL regression."""

import dataclasses
import functools
from typing import Callable
from flax import linen as nn
from flax import struct
import jax
import jax.numpy as jnp
import jaxtyping as jt
import numpy as np

Array = jnp.ndarray | np.ndarray

# NOTE: Lower initialization is **extremely** important. We need to start off
# with reasonably scaled output distribution and prevent exploding gradients /
# bad initial loss values. Also needs to be consistent across entire model in
# order to use the same learning rate.
default_kernel_init = nn.initializers.truncated_normal(stddev=0.02)
Dense = functools.partial(nn.Dense, kernel_init=default_kernel_init)
EPS = 1e-7
AnyTensor = jt.Float[jax.Array, '*A']


class Block(nn.Module):
  """Standard attention block with customizable mask."""

  d_model: int  # D
  num_heads: int  # H
  hidden_dim: int  # F
  dropout_rate: float

  def setup(self):
    self.pre_attn_norm = nn.LayerNorm()
    self.attn = nn.SelfAttention(
        num_heads=self.num_heads,
        qkv_features=self.d_model,
        dropout_rate=self.dropout_rate,
        kernel_init=default_kernel_init,
        out_kernel_init=default_kernel_init,
    )

    self.pre_ffw_norm = nn.LayerNorm()
    self.ffw = nn.Sequential(
        [Dense(self.hidden_dim), nn.relu, Dense(self.d_model)]
    )

    self.dropout = nn.Dropout(rate=self.dropout_rate)

  def __call__(
      self,
      x: jt.Float[jax.Array, 'B* L D'],
      mask: jt.Float[jax.Array, 'B* H QL KVL'] | None = None,
      deterministic: bool | None = None,
      rng: jax.Array | None = None,
  ) -> jt.Float[jax.Array, 'B* L D']:
    # Pre-attention normalization
    norm1 = self.pre_attn_norm(x)
    # Self-attention layer
    attn = self.attn(
        norm1, mask=mask, deterministic=deterministic, dropout_rng=rng
    )
    x = x + attn  # Residual connection
    # Pre-feed-forward normalization
    norm2 = self.pre_ffw_norm(x)
    # Feed-forward layer
    ff = self.ffw(norm2)
    x = x + ff  # Residual connection

    # Optionally, apply dropout
    if self.dropout_rate > 0.0:
      x = self.dropout(x, deterministic, rng)

    return x


@struct.dataclass
class EmbeddingCache:
  """Cache for storing previously computed embeddings."""

  x_emb: jt.Float[jax.Array, 'L E'] | None = None
  metadata_emb: jt.Float[jax.Array, 'E'] | None = None


class ICLTransformer(nn.Module):
  """ICL Transformer model for regression."""

  d_model: int  # D
  ffw_dim_ratio: int  # F // D
  nhead: int  # H
  dropout: float
  num_layers: int
  use_metadata: bool
  std_transform_fn: Callable[[AnyTensor], AnyTensor]
  embedder_factory: Callable[[], nn.Module]  # __call__: [B, T] -> [B, D]

  def setup(self):
    # For embedding x and metadata tokens.
    self.embedder = self.embedder_factory()

    # X, Y, and concatenated X,Y embedders.
    self.x_proj = nn.Sequential(
        [Dense(self.d_model), nn.relu, Dense(self.d_model)]
    )
    self.y_proj = nn.Sequential(
        [Dense(self.d_model), nn.relu, Dense(self.d_model)]
    )
    self.xy_proj = nn.Sequential(
        [Dense(self.d_model * 2), nn.relu, Dense(self.d_model)]
    )

    # Attention blocks with customizable masks.
    self.encoder_layers = [
        Block(
            d_model=self.d_model,
            num_heads=self.nhead,
            dropout_rate=self.dropout,
            hidden_dim=int(self.d_model * self.ffw_dim_ratio),
        )
        for _ in range(self.num_layers)
    ]

    # Predict mean and logstd.
    self.mean_logstd_head = nn.Sequential(
        [Dense(self.d_model), nn.relu, Dense(2)]
    )

  def __call__(
      self,
      x_emb: jt.Float[jax.Array, 'B L E'],
      y: jt.Float[jax.Array, 'B L'],
      mask: jt.Bool[jax.Array, 'B L'],
      deterministic: bool | None = None,
      rng: jax.Array | None = None,
  ) -> tuple[jt.Float[jax.Array, 'B L'], jt.Float[jax.Array, 'B L']]:
    """Main ICL Transformer call, **after** embeddings have been computed."""

    # pylint: disable=invalid-name
    L = x_emb.shape[1]

    x_emb = self.x_proj(x_emb)  # [B, L, D]

    # Force 0.0 values for target points using the mask.
    y = y * mask  # [B, L], element-wise multiplication

    y = jnp.expand_dims(y, axis=-1)  # [B, L, 1]
    yt_emb = self.y_proj(y)  # [B, L, D]
    xy_emb = self.xy_proj(jnp.concatenate((x_emb, yt_emb), axis=-1))

    # Broadcast mask to all heads and additional axis.
    # All tokens attend to context tokens: mask[:, :num_ctx] = True
    # and no token attends to target tokens: mask[:, num_ctx:] = False
    mask = jnp.repeat(jnp.expand_dims(mask, axis=1), L, axis=1)  # [B, L, L]
    mask = jnp.expand_dims(mask, axis=1)  # [B, 1, L, L]

    out = xy_emb
    for layer in self.encoder_layers:
      out = layer(out, mask, deterministic, rng)

    mean, std = jnp.split(self.mean_logstd_head(out), 2, axis=-1)  # [B L 1]
    std = self.std_transform_fn(std) + EPS

    mean = jnp.squeeze(mean, axis=-1)
    std = jnp.squeeze(std, axis=-1)
    return mean, std

  def fit(
      self,
      x: jt.Int[jax.Array, 'B L T'],  # T = number of tokens.
      y: jt.Float[jax.Array, 'B L'],
      metadata: jt.Int[jax.Array, 'B T'],  # Study-level tokenized metadata.
      mask: jt.Bool[jax.Array, 'B L'],
      deterministic: bool | None = None,
      rng: jax.Array | None = None,
  ) -> tuple[jt.Float[jax.Array, 'B L'], jt.Float[jax.Array, 'B L']]:
    """For training / eval loss metrics only."""
    x_emb = self.embed(x)  # [B, L, E]

    if self.use_metadata:
      L = x_emb.shape[1]  # pylint: disable=invalid-name
      metadata_emb = self.embed(metadata)  # [B, E]
      metadata_emb = jnp.expand_dims(metadata_emb, axis=1)  # [B, 1, E]
      metadata_emb = jnp.repeat(metadata_emb, L, axis=1)  # [B, L, E]
      x_emb = jnp.concatenate((x_emb, metadata_emb), axis=-1)  # [B, L, 2E]

    return self.__call__(x_emb, y, mask, deterministic, rng)

  def infer(
      self,
      x_padded: jt.Int[jax.Array, 'L T'],  # Padded to avoid re-jitting.
      y_padded: jt.Float[jax.Array, 'L'],  # Padded to avoid re-jitting.
      x_targ: jt.Int[jax.Array, 'Q T'],  # Q is fixed to avoid re-jitting.
      metadata: jt.Int[jax.Array, 'T'],
      mask: jt.Bool[jax.Array, 'L'],
      cache: EmbeddingCache,  # For caching embeddings.
  ) -> tuple[
      jt.Float[jax.Array, 'L'],
      jt.Float[jax.Array, 'L'],
      EmbeddingCache,
  ]:
    """Friendly for inference, no batch dimension."""
    if cache.x_emb is None:
      cache = dataclasses.replace(cache, x_emb=self.embed(x_padded))
    x_pad_emb = cache.x_emb  # [L, E]
    x_targ_emb = self.embed(x_targ)  # [Q, E]

    # Combine target and historical (padded) embeddings.
    target_index = jnp.sum(mask, dtype=jnp.int32)  # [1]
    padded_target_emb = jnp.zeros_like(x_pad_emb)
    padded_target_emb = jax.lax.dynamic_update_slice_in_dim(
        padded_target_emb, x_targ_emb, start_index=target_index, axis=0
    )
    w_mask = jnp.expand_dims(mask, axis=-1)  # [L, 1]
    x_emb = x_pad_emb * w_mask + padded_target_emb * (1 - w_mask)  # [L, E]

    if self.use_metadata:  # Attach metadata embeddings too.
      if cache.metadata_emb is None:
        cache = dataclasses.replace(cache, metadata_emb=self.embed(metadata))
      metadata_emb = cache.metadata_emb  # [E]
      metadata_emb = jnp.expand_dims(metadata_emb, axis=0)  # [1, E]
      metadata_emb = jnp.repeat(metadata_emb, x_emb.shape[0], axis=0)  # [L, E]
      x_emb = jnp.concatenate((x_emb, metadata_emb), axis=-1)  # [L, 2E]

    mean, std = self.__call__(
        x_emb=jnp.expand_dims(x_emb, axis=0),
        y=jnp.expand_dims(y_padded, axis=0),
        mask=jnp.expand_dims(mask, axis=0),
        deterministic=True,
    )
    return jnp.squeeze(mean, axis=0), jnp.squeeze(std, axis=0), cache

  @nn.remat  # Reduce memory consumption during backward pass.
  def embed(
      self, tokens: jt.Int[jax.Array, '*X T']
  ) -> jt.Float[jax.Array, '*X E']:
    reshaped_tokens = jnp.reshape(tokens, (-1, tokens.shape[-1]))
    embeddings = self.embedder(reshaped_tokens)  # [-1, E]
    return jnp.reshape(embeddings, tokens.shape[:-1] + (embeddings.shape[-1],))
