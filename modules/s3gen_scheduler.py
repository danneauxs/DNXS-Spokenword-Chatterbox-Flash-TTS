"""Retained-token batching primitives for the S3Gen stage."""

from dataclasses import dataclass

import torch


S3GEN_MEL_HOP_SAMPLES = 480


@dataclass(frozen=True)
class RetainedT3Row:
    """Store one validated T3 result for later S3Gen scheduling."""

    chunk_index: int
    speech_tokens: torch.Tensor
    conditioning_key: str
    conditioning: dict
    n_cfm_timesteps: int = 2


@dataclass(frozen=True)
class S3GenBatch:
    """Describe one padded S3Gen invocation and its source rows."""

    rows: tuple[RetainedT3Row, ...]
    speech_tokens: torch.Tensor
    speech_token_lens: torch.Tensor
    conditioning: dict
    n_cfm_timesteps: int


def _validate_retained_row(row: RetainedT3Row) -> None:
    """Reject retained rows that cannot form a valid token batch."""
    if row.speech_tokens.ndim != 1:
        raise ValueError("Retained S3 speech tokens must be one-dimensional")
    if row.speech_tokens.dtype != torch.long:
        raise ValueError("Retained S3 speech tokens must use torch.long")
    if row.speech_tokens.numel() == 0:
        raise ValueError("Retained S3 speech token rows cannot be empty")


def build_s3gen_batch(
    rows: tuple[RetainedT3Row, ...],
    *,
    padding_token: int = 0,
) -> S3GenBatch:
    """Pad retained rows and create the S3Gen length mask vector.

    Args:
        rows: Rows sharing conditioning and S3Gen settings.
        padding_token: Valid codec ID used after each row's true length.

    Returns:
        Padded token tensor, true lengths, and metadata for one S3Gen call.

    Raises:
        ValueError: If rows are empty or have incompatible metadata.
    """
    if not rows:
        raise ValueError("Cannot build an empty S3Gen batch")
    for row in rows:
        _validate_retained_row(row)

    first = rows[0]
    if any(
        row.conditioning_key != first.conditioning_key
        or row.n_cfm_timesteps != first.n_cfm_timesteps
        for row in rows
    ):
        raise ValueError("S3Gen batch rows must share conditioning and settings")

    max_length = max(row.speech_tokens.numel() for row in rows)
    padded = torch.full(
        (len(rows), max_length),
        int(padding_token),
        dtype=torch.long,
        device=first.speech_tokens.device,
    )
    lengths = torch.empty(len(rows), dtype=torch.long, device=padded.device)
    for index, row in enumerate(rows):
        row_length = row.speech_tokens.numel()
        padded[index, :row_length] = row.speech_tokens
        lengths[index] = row_length

    return S3GenBatch(
        rows=rows,
        speech_tokens=padded,
        speech_token_lens=lengths,
        conditioning=first.conditioning,
        n_cfm_timesteps=first.n_cfm_timesteps,
    )


def schedule_s3gen_batches(
    rows: list[RetainedT3Row],
    max_batch_size: int,
    max_padded_tokens: int | None = None,
) -> list[S3GenBatch]:
    """Group retained rows by settings and bounded padded-token workload.

    Rows are sorted by token length within each conditioning group, then
    divided into bounded batches. Chunk indices remain attached to each row,
    allowing output order restoration after S3Gen completes.

    Args:
        rows: Validated T3 results waiting for S3Gen.
        max_batch_size: Maximum number of rows in one S3Gen call.
        max_padded_tokens: Maximum value of rows multiplied by the longest
            row in one S3Gen call. ``None`` disables this second limit.

    Returns:
        Ordered list of padded S3Gen batches.

    Raises:
        ValueError: If max_batch_size is not positive.
    """
    if max_batch_size < 1:
        raise ValueError("max_batch_size must be positive")
    if max_padded_tokens is not None and max_padded_tokens < 1:
        raise ValueError("max_padded_tokens must be positive when provided")

    grouped: dict[tuple[str, int], list[RetainedT3Row]] = {}
    for row in rows:
        _validate_retained_row(row)
        grouped.setdefault(
            (row.conditioning_key, row.n_cfm_timesteps),
            [],
        ).append(row)

    batches: list[S3GenBatch] = []
    for group_rows in grouped.values():
        ordered = sorted(
            group_rows,
            key=lambda row: (row.speech_tokens.numel(), row.chunk_index),
        )
        start = 0
        while start < len(ordered):
            first_length = ordered[start].speech_tokens.numel()
            end = start
            while end < len(ordered) and end - start < max_batch_size:
                candidate_length = max(
                    first_length,
                    ordered[end].speech_tokens.numel(),
                )
                candidate_rows = end - start + 1
                exceeds_token_budget = (
                    max_padded_tokens is not None
                    and candidate_rows * candidate_length > max_padded_tokens
                )
                if exceeds_token_budget:
                    break
                end += 1

            if end == start:
                row_length = ordered[start].speech_tokens.numel()
                raise ValueError(
                    "S3Gen row exceeds max_padded_tokens: "
                    f"row_length={row_length}, budget={max_padded_tokens}"
                )

            batch_rows = tuple(ordered[start:end])
            batches.append(build_s3gen_batch(batch_rows))
            start = end
    return batches


def run_padded_s3gen_batch(model, batch: S3GenBatch) -> list[torch.Tensor]:
    """Run one padded multi-row S3Gen batch and crop each waveform.

    The installed public S3Gen wrapper loops over rows. This adapter calls
    its batch-capable flow and HiFiGAN components directly, correcting the
    meanflow noise batch dimension and preserving each row's true duration.

    Args:
        model: Prepared ChatterboxFlashTTS model.
        batch: Padded S3Gen batch produced by :func:`build_s3gen_batch`.

    Returns:
        CPU waveform tensor for each source row, in batch row order.
    """
    s3gen = model.s3gen
    device = model.device
    tokens = batch.speech_tokens.to(device=device)
    lengths = batch.speech_token_lens.to(device=device)
    batch_size, max_tokens = tokens.shape
    conditioning = batch.conditioning

    noised_mels = None
    if s3gen.meanflow:
        noised_mels = torch.randn(
            batch_size,
            80,
            max_tokens * 2,
            dtype=s3gen.dtype,
            device=device,
        )

    output_mels, _ = s3gen.flow.inference(
        token=tokens,
        token_len=lengths,
        prompt_token=conditioning["prompt_token"],
        prompt_token_len=conditioning["prompt_token_len"],
        prompt_feat=conditioning["prompt_feat"],
        prompt_feat_len=conditioning["prompt_feat_len"],
        embedding=conditioning["embedding"],
        finalize=True,
        n_timesteps=batch.n_cfm_timesteps,
        noised_mels=noised_mels,
        meanflow=s3gen.meanflow,
    )
    output_mels = output_mels.to(dtype=s3gen.dtype)
    cache_source = torch.zeros(1, 1, 0, device=device, dtype=s3gen.dtype)
    output_wavs, _ = s3gen.mel2wav.inference(
        speech_feat=output_mels,
        cache_source=cache_source,
    )
    if not s3gen.training:
        output_wavs[:, :len(s3gen.trim_fade)] *= s3gen.trim_fade

    waveforms = []
    mel_ratio = int(s3gen.flow.token_mel_ratio)
    for row_index, row in enumerate(batch.rows):
        expected_samples = (
            row.speech_tokens.numel()
            * mel_ratio
            * S3GEN_MEL_HOP_SAMPLES
        )
        waveforms.append(
            output_wavs[row_index, :expected_samples].detach().cpu()
        )
    return waveforms
