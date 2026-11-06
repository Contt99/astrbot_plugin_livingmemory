"""Shared date-only record evidence, never a birthday or birth claim."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import math
import re
import time

HEADER = '<LivingMemory-Temporal-Coverage>'
FOOTER = '</LivingMemory-Temporal-Coverage>'
CST = timezone(timedelta(hours=8))
_QUESTION = re.compile(
    r'(?:最早|最初|从什么时候开始).{0,24}(?:记忆|回忆|记录)'
    r'|(?:记忆|回忆|记录).{0,24}(?:最早|最初|从什么时候开始)'
    r'|从什么时候开始(?:能|会|还)?记得'
    r'|(?:最早|最初)(?:你|我|还|能|所|的){0,4}记得(?:什么|哪|几|的(?:事|一天|经历))'
    r'|记得.{0,6}(?:最早|最初)的?(?:事|一天|经历|记忆|回忆|哪天|日子)'
)


def is_temporal_question(query) -> bool:
    if not isinstance(query, str) or len(query) > 2000:
        return False
    return bool(_QUESTION.search(re.sub(r'\s+', '', query)))


def source_timestamp(value, *, now=None):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            value = value.strip()
            try:
                number = float(value)
            except ValueError:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=CST)
                number = parsed.timestamp()
        elif isinstance(value, (int, float)):
            number = float(value)
        else:
            return None
        if number > 100_000_000_000:
            number /= 1000.0
        upper = (time.time() if now is None else now) + 86400
        if not math.isfinite(number) or not 946684800 <= number <= upper:
            return None
        return number
    except (ValueError, OverflowError, OSError):
        return None


def record_range(metadata):
    """Only structured source times or audited QQ Agent block anchors qualify."""
    source = metadata.get('source')
    kinds = {str(metadata.get(key) or '').casefold()
             for key in ('memory_type', 'interaction_type')}
    if source == 'qqagent_backup' or kinds.intersection(('profile', 'profile_backup', 'person_profile')):
        return None
    if 'source_time_start' in metadata:
        start = source_timestamp(metadata['source_time_start'])
        end = source_timestamp(metadata.get('source_time_end', metadata['source_time_start']))
        if start is None or end is None or end < start:
            return None
        return start, end, 'source_event'
    if (source != 'qqagent_memory_v3' or not metadata.get('block_id')
            or metadata.get('block_kind') not in ('day', 'hour')):
        return None
    try:
        day = date.fromisoformat(metadata.get('day', ''))
        anchor = source_timestamp(metadata.get('create_time'))
        if anchor is None or datetime.fromtimestamp(anchor, CST).date() != day:
            return None
    except (ValueError, TypeError):
        return None
    return anchor, anchor, 'qqagent_block_anchor'


def summarize_records(metadata_rows):
    ranges = [record_range(md) for md in metadata_rows]
    known = [r for r in ranges if r is not None]
    return {
        'status': 'ok' if known else 'unknown',
        'dated_record_count': len(known),
        'unknown_date_count': len(ranges) - len(known),
        'earliest_date': datetime.fromtimestamp(min(r[0] for r in known), CST).date().isoformat() if known else None,
        'latest_date': datetime.fromtimestamp(max(r[1] for r in known), CST).date().isoformat() if known else None,
        'basis_counts': dict(Counter(r[2] for r in known)),
        'coverage': 'current_visible_active_records',
        'completeness': 'stored_subset_not_first_existence',
    }


def format_coverage(result):
    if not isinstance(result, dict) or result.get('status') not in ('ok', 'unknown'):
        return ''
    if result['status'] == 'ok':
        # The producer is local, but validate its allowed output again at injection.
        try:
            first = date.fromisoformat(result['earliest_date']).isoformat()
            last = date.fromisoformat(result['latest_date']).isoformat()
            count = int(result['dated_record_count'])
            if first > last or not 1 <= count <= 100000:
                return ''
        except (KeyError, TypeError, ValueError):
            return ''
        evidence = f'当前会话有权可见、仍有效且可定时的 {count} 份记忆记录，覆盖日期为 {first} 至 {last}（北京时间）。'
    else:
        evidence = '当前会话可访问的记录缺少可靠的来源日期，最早日期未知；不能拿建档或迁移日期代替。'
    body = (evidence + '历史摘要只提供对应日期的记录锚点，不代表完整原话的第一秒。'
            '这个范围不是生日、首次认识或首次存在的证明，也不排除更早经历。'
            '少量相关命中不代表全部时间覆盖；生日应单独核实，证据不足就说不确定。')
    return f'{HEADER}\n{body}\n{FOOTER}'
