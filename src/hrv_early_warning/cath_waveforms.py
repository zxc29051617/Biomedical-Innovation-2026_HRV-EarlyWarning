"""Bounded, local WFDB decoding for the public pre-angiography ECG audit.

This module reports engineering properties, not clinical ECG quality or OMI.
WFDB performs the binary decoding and physical calibration. Header text and
source record identifiers never enter the returned metadata or review case.
See https://wfdb.readthedocs.io/en/latest/io.html for rdheader/rdrecord semantics.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import tempfile
import zipfile


LEAD_NAMES = ("I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6")
EXPECTED_FS = 500
RAW_EXPECTED_SAMPLES = 5000
MAX_SAMPLES = 5000
MAX_MEMBERS = 100_000
MAX_HEADER_BYTES = 64 * 1024
MAX_DATA_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 8 * 1024**3
UNIT_TO_MV = {"mV": 1.0, "uV": 0.001, "µV": 0.001, "μV": 0.001}
_CALIBRATION = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?:\(([+-]?\d+)\))?/([^\s]+)")
_RECORD_BASENAME = re.compile(r"[A-Za-z0-9_-]+")
_DATA_BASENAME = re.compile(r"[A-Za-z0-9_.-]+\.dat")
_CASE_ID = re.compile(r"cr-[0-9a-f]{32}")


class CathWaveformError(ValueError):
    """A safe machine-readable failure; details contain numeric aggregates only."""

    def __init__(self, code, **details):
        super().__init__(code)
        self.code = code
        self.details = details


# Public short name used by the archive audit runner.
WaveformError = CathWaveformError


def _numpy():
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError("Cath waveform analysis requires optional numpy") from None
    return np


def _wfdb():
    try:
        import wfdb
    except ImportError:
        raise RuntimeError("Cath waveform decoding requires optional wfdb") from None
    return wfdb


def _safe_name(name, *, directory=False):
    if not isinstance(name, str) or not name or len(name) > 1024:
        raise CathWaveformError("unsafe_archive_path")
    if "\\" in name or ":" in name or any(ord(c) < 32 for c in name):
        raise CathWaveformError("unsafe_archive_path")
    value = name[:-1] if directory and name.endswith("/") else name
    if value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/")):
        raise CathWaveformError("unsafe_archive_path")
    return value


def _representation(value):
    if value not in ("raw", "median"):
        raise CathWaveformError("unknown_representation")
    return value


def _validated_array(waveform, lead_names, fs):
    np = _numpy()
    if list(lead_names) != list(LEAD_NAMES):
        raise CathWaveformError("review_requires_canonical_lead_order")
    if isinstance(fs, bool) or not isinstance(fs, (int, float)) or fs != EXPECTED_FS:
        raise CathWaveformError("unexpected_sampling_rate")
    try:
        signal = np.asarray(waveform, dtype=np.float64)
    except (TypeError, ValueError, OverflowError):
        raise CathWaveformError("invalid_waveform_array") from None
    if signal.ndim != 2 or signal.shape[1] != 12 or not 2 <= signal.shape[0] <= MAX_SAMPLES:
        raise CathWaveformError("invalid_waveform_dimensions")
    missing = int(np.count_nonzero(~np.isfinite(signal)))
    if missing:
        raise CathWaveformError("nonfinite_samples", nonfinite_samples=missing,
                                affected_leads=int(np.any(~np.isfinite(signal), axis=0).sum()))
    return signal


def waveform_fingerprint(waveform, lead_names=LEAD_NAMES, fs=EXPECTED_FS):
    """Hash canonical decoded mV samples, lead order, shape and fs, not source files."""
    np = _numpy()
    signal = _validated_array(waveform, lead_names, fs)
    digest = hashlib.sha256(b"cath-waveform-mv-v1\0")
    digest.update("\0".join(LEAD_NAMES).encode("ascii"))
    digest.update(struct.pack("<dII", float(fs), *signal.shape))
    digest.update(np.asarray(signal, dtype="<f8", order="C").tobytes(order="C"))
    return digest.hexdigest()


class ZipRecordReader:
    """Index a ZIP once; read only bounded headers and their local data members.

    The caller owns the ZipFile lifetime. No archive path is extracted directly:
    validated record members are copied into a private temporary directory and
    removed after the official WFDB reader finishes.
    """

    def __init__(self, zf: zipfile.ZipFile):
        self.zf = zf
        self.members = {}
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise CathWaveformError("archive_member_limit")
        total = 0
        occupied = set()
        for info in infos:
            name = _safe_name(info.filename, directory=info.is_dir())
            if name in occupied:
                raise CathWaveformError("duplicate_archive_member")
            occupied.add(name)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise CathWaveformError("nonregular_archive_member")
            if info.flag_bits & 1:
                raise CathWaveformError("encrypted_archive_member")
            if info.file_size < 0 or info.file_size > MAX_DATA_BYTES:
                raise CathWaveformError("archive_member_size_limit")
            total += info.file_size
            if total > MAX_ARCHIVE_EXPANDED_BYTES:
                raise CathWaveformError("archive_expansion_limit")
            if not info.is_dir():
                self.members[name] = info

    def _read_member(self, name, limit):
        info = self.members.get(name)
        if info is None:
            raise CathWaveformError("record_member_missing")
        if info.file_size > limit:
            raise CathWaveformError("record_member_size_limit")
        try:
            with self.zf.open(info) as stream:
                data = stream.read(limit + 1)
        except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
            raise CathWaveformError("archive_member_unreadable") from None
        if len(data) > limit or len(data) != info.file_size:
            raise CathWaveformError("record_member_size_mismatch")
        return data

    def read_record(self, record_path, *, representation="raw"):
        """Return (canonical float64 mV [samples,12], aggregate-safe metadata).

        record_path is the exact archive-relative stem or .hea path. Sample
        counts other than raw 5000 are reported, not padded or resampled. Median
        beats have no assumed duration beyond their recorded sample count/fs.
        """
        _representation(representation)
        name = _safe_name(record_path)
        if name.endswith(".hea"):
            name = name[:-4]
        record = PurePosixPath(name)
        if not _RECORD_BASENAME.fullmatch(record.name):
            raise CathWaveformError("unsafe_record_name")
        data = self._read_member(name + ".hea", MAX_HEADER_BYTES)
        try:
            text = data.decode("utf-8-sig")
            lines = [line.strip() for line in text.splitlines()
                     if line.strip() and not line.lstrip().startswith("#")]
            fields = [line.split() for line in lines]
            if not fields or len(fields[0]) < 4:
                raise CathWaveformError("invalid_record_header")
            if fields[0][0] != record.name or "/" in fields[0][0]:
                raise CathWaveformError("record_header_name_mismatch")
            if int(fields[0][1]) != 12 or len(fields) != 13:
                raise CathWaveformError("unexpected_header_signal_count")
            length = int(fields[0][3])
            if not 2 <= length <= MAX_SAMPLES:
                raise CathWaveformError("header_sample_limit")
            file_names = set()
            for field in fields[1:]:
                if len(field) < 9:
                    raise CathWaveformError("explicit_signal_metadata_required")
                if not _DATA_BASENAME.fullmatch(field[0]) or field[0].startswith("."):
                    raise CathWaveformError("unsafe_signal_reference")
                file_names.add(field[0])
        except CathWaveformError:
            raise
        except (UnicodeError, ValueError, TypeError, IndexError):
            raise CathWaveformError("invalid_record_header") from None
        # WFDB header parsing is local and cannot follow archive-provided paths:
        # all names were checked above and multi-segment records are rejected.
        np, wfdb = _numpy(), _wfdb()
        try:
            with tempfile.TemporaryDirectory(prefix="cath-ecg-") as temporary:
                folder = Path(temporary)
                base = folder / record.name
                header_path = base.with_suffix(".hea")
                header_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                header_path.chmod(0o600)
                header = wfdb.rdheader(str(base), rd_segments=False)
                order, factors, calibration = self._header_metadata(header, fields)
                total = len(data)
                for filename in sorted(file_names):
                    member_name = str(record.parent / filename)
                    payload = self._read_member(member_name, min(MAX_DATA_BYTES, MAX_RECORD_BYTES - total))
                    total += len(payload)
                    target = folder / filename
                    target.write_bytes(payload)
                    target.chmod(0o600)
                decoded = wfdb.rdrecord(str(base), physical=True, return_res=64,
                                        smooth_frames=True, ignore_skew=False)
                signal = np.asarray(decoded.p_signal, dtype=np.float64)
                if signal.shape != (length, 12):
                    raise CathWaveformError("decoded_shape_mismatch")
                signal = signal[:, order] * np.asarray(factors)[None, :]
        except CathWaveformError:
            raise
        except Exception:
            raise CathWaveformError("wfdb_decode_error") from None
        signal = _validated_array(signal, LEAD_NAMES, float(header.fs))
        per_lead = []
        for index, lead in enumerate(LEAD_NAMES):
            values = signal[:, index]
            minimum, maximum = float(values.min()), float(values.max())
            per_lead.append({"lead": lead, "minimum_mv": minimum, "maximum_mv": maximum,
                             "peak_to_peak_mv": maximum - minimum,
                             "flat_exact": minimum == maximum, "nonfinite_samples": 0})
        # Optional clock fields in the original first header line are presence
        # flags only. WFDB defaults are not counted as supplied clock metadata.
        optional = fields[0][4:]
        has_time = bool(optional and re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?", optional[0]))
        has_date = any(re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", item) for item in optional)
        metadata = {"schema_version": "cath-waveform-structural-v1", "lead_names": list(LEAD_NAMES),
                    "source_lead_names": list(header.sig_name), "fs": float(header.fs),
                    "units": ["mV"] * 12, **calibration, "nsamples": length,
                    "representation": representation, "duration_seconds": length / float(header.fs),
                    "raw_expected_length_matches": length == RAW_EXPECTED_SAMPLES if representation == "raw" else None,
                    "header_has_base_time": has_time, "header_has_base_date": has_date,
                    "waveform_sha256": waveform_fingerprint(signal),
                    "fingerprint_definition": "cath-waveform-mv-v1: canonical decoded mV samples, leads, shape and fs",
                    "nonfinite_samples": 0, "per_lead": per_lead,
                    "clinical_quality_assessed": False}
        return signal, metadata

    @staticmethod
    def _header_metadata(header, fields):
        if hasattr(header, "seg_name") or header.n_sig != 12:
            raise CathWaveformError("multisegment_or_invalid_signal_count")
        if header.fs != EXPECTED_FS:
            raise CathWaveformError("unexpected_sampling_rate")
        names = list(header.sig_name or [])
        if len(names) != 12 or set(names) != set(LEAD_NAMES):
            raise CathWaveformError("unexpected_leads")
        order = [names.index(lead) for lead in LEAD_NAMES]
        factors, units, gains, baselines, formats = [], [], [], [], []
        for index in order:
            field = fields[index + 1]
            match = _CALIBRATION.fullmatch(field[2])
            if match is None:
                raise CathWaveformError("explicit_calibration_required")
            gain = float(match.group(1))
            baseline = int(match.group(2)) if match.group(2) is not None else int(field[4])
            unit = match.group(3)
            if not math.isfinite(gain) or gain <= 0:
                raise CathWaveformError("invalid_adc_gain")
            if unit not in UNIT_TO_MV:
                raise CathWaveformError("unsupported_voltage_unit")
            if (header.adc_gain[index] != gain or header.baseline[index] != baseline
                    or header.units[index] != unit or header.file_name[index] != field[0]
                    or " ".join(field[8:]) != names[index]):
                raise CathWaveformError("inconsistent_calibration_metadata")
            if (header.samps_per_frame or [1] * 12)[index] != 1:
                raise CathWaveformError("multiple_samples_per_frame_unsupported")
            if (header.skew or [0] * 12)[index] not in (0, None):
                raise CathWaveformError("nonzero_skew_unsupported")
            factors.append(UNIT_TO_MV[unit])
            units.append(unit)
            gains.append(gain)
            baselines.append(baseline)
            formats.append(str(header.fmt[index]))
        return order, factors, {"source_units": units, "unit_scale_to_mv": factors,
                                "adc_gain": gains, "baseline": baselines, "format": formats}


def read_record_from_zip(zf, record_path, *, representation="raw"):
    """Convenience for a single record; reuse ZipRecordReader for many records."""
    return ZipRecordReader(zf).read_record(record_path, representation=representation)


def build_blinded_review_case(waveform, lead_names, fs, age, sex, representation,
                              case_id, waveform_sha256):
    """Create only the fixed review schema, with no diagnosis or source IDs.

    The caller must generate a random case_id and keep its source mapping in an
    ignored local file. The waveform fingerprint is recomputed, not trusted.
    """
    _representation(representation)
    if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
        raise CathWaveformError("invalid_blinded_case_id")
    if sex not in ("male", "female", "unknown"):
        raise CathWaveformError("invalid_review_sex")
    if age is not None and (isinstance(age, bool) or not isinstance(age, (int, float))
                            or not math.isfinite(age) or not 0 <= age <= 130):
        raise CathWaveformError("invalid_review_age")
    signal = _validated_array(waveform, lead_names, fs)
    expected = waveform_fingerprint(signal, lead_names, fs)
    if not isinstance(waveform_sha256, str) or waveform_sha256 != expected:
        raise CathWaveformError("waveform_fingerprint_mismatch")
    return {"schema_version": "cath-ecg-review-v1", "case_id": case_id,
            "age_years": age, "sex": sex, "sample_rate_hz": float(fs),
            "lead_names": list(LEAD_NAMES), "signals_mv": signal.tolist(),
            "representation": representation,
            "provenance": {"waveform_sha256": expected}}
