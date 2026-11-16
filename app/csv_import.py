"""累计抄码 CSV 解析与校验。

三列：计量点、UTC 整点、非负累计电量（kWh）。有表头时按列名映射（支持中英文
别名，列顺序任意，允许额外的说明列）；无表头时按前三列固定顺序解析。

解析阶段只做与数据库无关的校验：编码、列数、时间整点、电量有限非负、
单文件最多 500 行、同一文件内同点同刻示数必须一致。逐行保留 CSV 物理行号，
供追溯“来源批次 + 行号”使用。
"""
from __future__ import annotations

import csv
import io
import math
import re
import unicodedata
from dataclasses import dataclass
from .schemas import DomainError, validate_hour

MAX_ROWS = 500

# 常见网关导出时间写法：带 T 或空格、可省略秒、可带 Z / +00:00
_HOUR_INPUT_RE = re.compile(
    r"^(?P<d>\d{4}-\d{2}-\d{2})[T ](?P<t>\d{2}:\d{2})(?::(?P<s>\d{2}))?"
    r"(?:\s*(?P<z>Z|z|\+00:00|\+0000))?$"
)

_POINT_ALIASES = {
    "point_id", "point", "meter_point", "meter", "p", "计量点", "计量点编号",
    "计量点号", "点号", "表号",
}
_HOUR_ALIASES = {
    "hour", "utc_hour", "hour_utc", "time", "timestamp", "utc整点", "整点",
    "小时", "时间", "抄码时间", "utc时间", "utc小时",
}
_CUMULATIVE_ALIASES = {
    "cumulative_kwh", "cumulative_energy", "cumulative", "reading", "value",
    "kwh", "累计电量", "累计示数", "示数", "抄码", "表底", "累计电量kwh",
    "累计电量(kwh)", "累计示数(kwh)", "抄见示数", "累计读数",
}


@dataclass(frozen=True)
class ParsedRow:
    line_no: int
    point_id: str
    hour: str
    cumulative_kwh: float
    first_in_file: bool


@dataclass(frozen=True)
class ParsedCsv:
    rows: list[ParsedRow]


def distinct_key_count(parsed: ParsedCsv) -> int:
    return len({(r.point_id, r.hour) for r in parsed.rows})


def _norm_header(value: str) -> str:
    v = unicodedata.normalize("NFKC", value or "").strip().lower()
    return "".join(v.split())


def _norm_point(value: str, line_no: int) -> str:
    v = (value or "").strip()
    if not v:
        raise DomainError(f"CSV 第 {line_no} 行计量点为空")
    if len(v) > 64:
        raise DomainError(f"CSV 第 {line_no} 行计量点超过 64 个字符")
    return v


def _norm_hour(value: str, line_no: int) -> str:
    """把网关常见时间写法归一化为 UTC 整点；非整点/带时区偏移一律拒收。"""
    v = (value or "").strip()
    if not v:
        raise DomainError(f"CSV 第 {line_no} 行 UTC 整点为空")
    m = _HOUR_INPUT_RE.match(v)
    if not m:
        raise DomainError(
            f"CSV 第 {line_no} 行时间必须为 UTC 整点，如 2026-11-15T08:00:00Z，收到 {value!r}"
        )
    seconds = m.group("s")
    if seconds is not None and int(seconds) != 0:
        raise DomainError(f"CSV 第 {line_no} 行时间必须落在整点，收到 {value!r}")
    canonical = f"{m.group('d')}T{m.group('t')}:00Z"
    try:
        validate_hour(canonical)
    except DomainError as exc:
        raise DomainError(f"CSV 第 {line_no} 行：{exc.message}") from exc
    return canonical


def _parse_cumulative(value: str, line_no: int) -> float:
    v = (value or "").strip()
    if not v:
        raise DomainError(f"CSV 第 {line_no} 行累计电量为空")
    try:
        num = float(v)
    except ValueError as exc:
        raise DomainError(f"CSV 第 {line_no} 行累计电量不是数字: {value!r}") from exc
    if not math.isfinite(num):
        raise DomainError(f"CSV 第 {line_no} 行累计电量必须是有限数，不接受 NaN/Infinity")
    if num < 0:
        raise DomainError(f"CSV 第 {line_no} 行累计电量必须非负，收到 {num}")
    return num


def _looks_like_header(record: list[str]) -> bool:
    """表头判定：三列中“整点”和“累计电量”都无法按数据解析。"""
    if len(record) < 3:
        return False
    cells = [c or "" for c in record]
    hour_ok = False
    try:
        _norm_hour(cells[1], 0)
        hour_ok = True
    except DomainError:
        pass
    cum_ok = False
    try:
        float(cells[2])
        cum_ok = True
    except ValueError:
        pass
    return not hour_ok and not cum_ok


def _map_columns(header: list[str]) -> tuple[int, int, int]:
    point_idx = hour_idx = cum_idx = -1
    for idx, name in enumerate(header):
        if name in _POINT_ALIASES and point_idx == -1:
            point_idx = idx
        elif name in _HOUR_ALIASES and hour_idx == -1:
            hour_idx = idx
        elif name in _CUMULATIVE_ALIASES and cum_idx == -1:
            cum_idx = idx
    if point_idx == -1 and hour_idx == -1 and cum_idx == -1:
        # 表头无可识别列名：按规范的三列固定顺序（计量点、整点、累计电量）
        if len(header) < 3:
            raise DomainError("CSV 至少需要 3 列：计量点、UTC 整点、累计电量")
        return 0, 1, 2
    if -1 in (point_idx, hour_idx, cum_idx):
        raise DomainError(
            "CSV 表头必须包含计量点、UTC 整点、累计电量三列"
            "（可写作 point_id,hour,cumulative_kwh 或 计量点,整点,累计电量）"
        )
    return point_idx, hour_idx, cum_idx


def parse_meter_csv(data: bytes) -> ParsedCsv:
    if not data:
        raise DomainError("上传的 CSV 为空")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DomainError("CSV 必须为 UTF-8 编码") from exc

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        records: list[tuple[int, list[str]]] = []
        for record in reader:
            line_no = reader.line_num
            if not record or all(not (c or "").strip() for c in record):
                continue
            records.append((line_no, record))
    except csv.Error as exc:
        raise DomainError(f"CSV 格式错误: {exc}") from exc

    if not records:
        raise DomainError("CSV 为空或缺少表头")

    first_line, first_record = records[0]
    if _looks_like_header(first_record):
        header = [_norm_header(c) for c in first_record]
        point_idx, hour_idx, cum_idx = _map_columns(header)
        column_count = len(first_record)
        data_records = records[1:]
    else:
        if len(first_record) != 3:
            raise DomainError("无表头 CSV 必须恰好包含三列：计量点、UTC 整点、累计电量")
        point_idx, hour_idx, cum_idx = 0, 1, 2
        column_count = 3
        data_records = records

    if not data_records:
        raise DomainError("CSV 没有数据行")
    if len(data_records) > MAX_ROWS:
        raise DomainError(
            f"CSV 最多 {MAX_ROWS} 行数据，实际 {len(data_records)} 行，整批拒收"
        )

    rows: list[ParsedRow] = []
    seen: dict[tuple[str, str], tuple[float, int]] = {}
    for line_no, record in data_records:
        if len(record) != column_count:
            raise DomainError(
                f"CSV 第 {line_no} 行应有 {column_count} 列，实际 {len(record)} 列"
            )
        point_id = _norm_point(record[point_idx], line_no)
        hour = _norm_hour(record[hour_idx], line_no)
        cumulative = _parse_cumulative(record[cum_idx], line_no)

        key = (point_id, hour)
        is_first = key not in seen
        if key in seen:
            prev_value, prev_line = seen[key]
            if prev_value != cumulative:
                raise DomainError(
                    f"CSV 内计量点 {point_id} 在 {hour} 的示数不一致"
                    f"（第 {prev_line} 行 {prev_value}，第 {line_no} 行 {cumulative}），整批拒收"
                )
        else:
            seen[key] = (cumulative, line_no)

        rows.append(ParsedRow(
            line_no=line_no,
            point_id=point_id,
            hour=hour,
            cumulative_kwh=cumulative,
            first_in_file=is_first,
        ))

    return ParsedCsv(rows=rows)
