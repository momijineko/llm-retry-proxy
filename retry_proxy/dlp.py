import json
import re
from dataclasses import dataclass
from functools import lru_cache

import yaml


_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"
_FLAGS = {"IGNORECASE": re.IGNORECASE, "MULTILINE": re.MULTILINE, "DOTALL": re.DOTALL}


@dataclass(frozen=True)
class DlpResult:
    body: bytes
    matched_rules: tuple[str, ...]
    exemptions: int
    malformed_exemption: bool = False
    redactions: int = 0


@dataclass(frozen=True)
class DlpPolicy:
    version: int
    rules: dict
    sensitive_json_keys: frozenset


@lru_cache(maxsize=8)
def load_policy(path):
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle) if path.lower().endswith(".json") else yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("DLP rule file must contain an object")
    if raw.get("version") != 1 or not isinstance(raw.get("rules"), dict):
        raise ValueError("unsupported or malformed DLP rule file")
    rules = {}
    for name, definition in raw["rules"].items():
        if not isinstance(definition, dict) or not definition.get("pattern"):
            raise ValueError(f"DLP rule {name!r} has no pattern")
        flags = 0
        for flag in definition.get("flags", []):
            if flag not in _FLAGS:
                raise ValueError(f"DLP rule {name!r} uses unknown regex flag {flag!r}")
            flags |= _FLAGS[flag]
        validator = definition.get("validator", "")
        if validator not in ("", "cn_id_checksum", "luhn"):
            raise ValueError(f"DLP rule {name!r} uses unknown validator {validator!r}")
        rules[name] = (re.compile(definition["pattern"], flags), validator)
    keys = raw.get("sensitive_json_keys", [])
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ValueError("sensitive_json_keys must be a string array")
    return DlpPolicy(1, rules, frozenset(key.lower() for key in keys))


def _valid_id_card(value):
    expected = _ID_CHECK[sum(int(n) * w for n, w in zip(value[:17], _ID_WEIGHTS)) % 11]
    return value[-1].upper() == expected


def _valid_bank_card(value):
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) < 13 or len(digits) > 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        number = int(char)
        if index % 2 == parity:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _inspect_text(value, enabled_rules, redact, policy):
    spans = []
    for name in enabled_rules & policy.rules.keys():
        pattern, validator = policy.rules[name]
        for match in pattern.finditer(value):
            valid = (not validator or
                     validator == "cn_id_checksum" and _valid_id_card(match.group()) or
                     validator == "luhn" and _valid_bank_card(match.group()))
            if valid:
                spans.append((match.start(), match.end(), name))
    matched = {item[2] for item in spans}
    if not redact or not spans:
        return value, matched, 0
    output = []
    position = 0
    redactions = 0
    for start, end, name in sorted(spans):
        if start < position:
            continue
        output.append(value[position:start])
        output.append(f"[REDACTED:{name}]")
        position = end
        redactions += 1
    output.append(value[position:])
    return "".join(output), matched, redactions


def _process_text(value, start_marker, end_marker, strip_markers, enabled_rules, redact, policy):
    output = []
    matched = set()
    exemptions = 0
    redactions = 0
    position = 0
    while position < len(value):
        start = value.find(start_marker, position)
        if start < 0:
            cleaned, found, count = _inspect_text(value[position:], enabled_rules, redact, policy)
            matched.update(found)
            redactions += count
            output.append(cleaned)
            break
        cleaned, found, count = _inspect_text(value[position:start], enabled_rules, redact, policy)
        matched.update(found)
        redactions += count
        output.append(cleaned)
        end = value.find(end_marker, start + len(start_marker))
        if end < 0:
            cleaned, found, count = _inspect_text(value[start:], enabled_rules, redact, policy)
            matched.update(found)
            redactions += count
            output.append(cleaned)
            break
        content_start = start + len(start_marker)
        content = value[content_start:end]
        if start_marker in content:
            cleaned, found, count = _inspect_text(value[start:end + len(end_marker)], enabled_rules,
                                                   redact, policy)
            matched.update(found)
            redactions += count
            output.append(cleaned)
            position = end + len(end_marker)
            continue
        exemptions += 1
        if strip_markers:
            output.append(content)
        else:
            output.append(start_marker + content + end_marker)
        position = end + len(end_marker)
    return "".join(output), matched, exemptions, False, redactions


def inspect_json_body(body, enabled_rules, start_marker, end_marker, strip_markers=True, redact=False,
                      rule_file=""):
    if not body:
        return DlpResult(body, (), 0)
    if not start_marker or not end_marker or start_marker == end_marker:
        return DlpResult(body, (), 0, True)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return DlpResult(body, (), 0)

    policy = load_policy(rule_file)
    matched = set()
    exemptions = 0
    redactions = 0
    malformed = False

    def visit(value):
        nonlocal exemptions, malformed, redactions
        if isinstance(value, str):
            cleaned, found, count, invalid, replaced = _process_text(
                value, start_marker, end_marker, strip_markers, enabled_rules, redact, policy
            )
            matched.update(found)
            exemptions += count
            redactions += replaced
            malformed |= invalid
            return cleaned
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            output = {}
            for key, item in value.items():
                cleaned = visit(item)
                if ("structured_secret" in enabled_rules and key.lower() in policy.sensitive_json_keys
                        and isinstance(item, str) and item and "[REDACTED:" not in cleaned):
                    stripped = item.strip()
                    if stripped.startswith(start_marker) and stripped.endswith(end_marker):
                        output[key] = cleaned
                    else:
                        matched.add("structured_secret")
                        redactions += 1 if redact else 0
                        output[key] = "[REDACTED:structured_secret]" if redact else cleaned
                else:
                    output[key] = cleaned
            return output
        return value

    def visit_latest_user(items):
        output = list(items)
        user_indexes = [index for index, item in enumerate(items)
                        if isinstance(item, dict) and item.get("role") == "user"]
        if user_indexes:
            index = user_indexes[-1]
            output[index] = visit(items[index])
        else:
            output = [visit(item) if isinstance(item, str) else item for item in items]
        return output

    if isinstance(payload, dict):
        cleaned = dict(payload)
        recognized = False
        if isinstance(payload.get("messages"), list):
            cleaned["messages"] = visit_latest_user(payload["messages"])
            recognized = True
        if "input" in payload:
            value = payload["input"]
            if isinstance(value, list):
                cleaned["input"] = visit_latest_user(value)
            elif isinstance(value, (str, dict)):
                cleaned["input"] = visit(value)
            recognized = True
        for key in ("prompt", "query"):
            if isinstance(payload.get(key), str):
                cleaned[key] = visit(payload[key])
                recognized = True
        if not recognized:
            cleaned = visit(payload)
    else:
        cleaned = visit(payload)
    encoded = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return DlpResult(encoded, tuple(sorted(matched)), exemptions, malformed, redactions)
