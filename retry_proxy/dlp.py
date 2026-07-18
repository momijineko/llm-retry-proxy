import base64
import binascii
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote_to_bytes

import yaml


_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"
_FLAGS = {"IGNORECASE": re.IGNORECASE, "MULTILINE": re.MULTILINE, "DOTALL": re.DOTALL}
_ACTIONS = {"audit", "redact", "block"}
_ACTION_PRIORITY = {"audit": 1, "redact": 2, "block": 3}
_VALIDATORS = {"", "cn_id_checksum", "luhn"}
_BINARY_KEYS = {"image", "image_url", "audio", "file_data", "data", "blob"}
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_BASE64_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9_+/-])[A-Za-z0-9_+/-]{16,}={0,2}(?![A-Za-z0-9_+/-])"
)
_HEX_CANDIDATE = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}){16,}(?![0-9A-Fa-f])")
_PERCENT_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9._~%-])(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2}){16,}(?![A-Za-z0-9._~%-])"
)


@dataclass(frozen=True)
class DlpRule:
    name: str
    pattern: object = None
    validator: str = ""
    keywords: tuple[str, ...] = ()
    min_entropy: float = 0.0
    action: str = ""
    placeholder: str = ""
    allowlist: tuple[str, ...] = ()
    max_matches: int = 100
    secret_group: int = 0
    enabled: bool = True
    json_keys: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DlpPolicy:
    version: int
    rules: dict[str, DlpRule]
    default_action: str
    default_placeholder: str


@dataclass(frozen=True)
class DlpResult:
    body: bytes
    matched_rules: tuple[str, ...]
    exemptions: int
    malformed_exemption: bool = False
    redactions: int = 0
    blocked_rules: tuple[str, ...] = ()
    audited_rules: tuple[str, ...] = ()
    uninspectable: bool = False
    limit_exceeded: bool = False


@dataclass
class _DecodeBudget:
    candidates: int
    bytes: int
    exhausted: bool = False


def _string_list(value, field, rule_name=""):
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        prefix = f"DLP rule {rule_name!r} " if rule_name else "DLP policy "
        raise ValueError(f"{prefix}{field} must be a string array")
    return tuple(value)


@lru_cache(maxsize=8)
def load_policy(path):
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle) if path.lower().endswith(".json") else yaml.safe_load(handle)
    if not isinstance(raw, dict) or raw.get("version") not in (1, 2):
        raise ValueError("unsupported or malformed DLP rule file")
    if not isinstance(raw.get("rules"), dict):
        raise ValueError("DLP rule file has no rules object")
    defaults = raw.get("defaults", {}) if raw.get("version") == 2 else {}
    if not isinstance(defaults, dict):
        raise ValueError("DLP defaults must be an object")
    default_action = defaults.get("action", "redact")
    default_placeholder = defaults.get("placeholder", "[REDACTED:{rule}]")
    if default_action not in _ACTIONS:
        raise ValueError(f"unknown default DLP action {default_action!r}")
    if not isinstance(default_placeholder, str) or "{rule}" not in default_placeholder:
        raise ValueError("DLP default placeholder must contain {rule}")

    rules = {}
    for name, definition in raw["rules"].items():
        if not isinstance(definition, dict):
            raise ValueError(f"DLP rule {name!r} must be an object")
        flags = 0
        for flag in _string_list(definition.get("flags"), "flags", name):
            if flag not in _FLAGS:
                raise ValueError(f"DLP rule {name!r} uses unknown regex flag {flag!r}")
            flags |= _FLAGS[flag]
        pattern_text = definition.get("pattern")
        json_keys = frozenset(key.lower() for key in _string_list(definition.get("json_keys"), "json_keys", name))
        if not pattern_text and not json_keys:
            raise ValueError(f"DLP rule {name!r} needs pattern or json_keys")
        if pattern_text is not None and not isinstance(pattern_text, str):
            raise ValueError(f"DLP rule {name!r} pattern must be a string")
        validator = definition.get("validator", "")
        if validator not in _VALIDATORS:
            raise ValueError(f"DLP rule {name!r} uses unknown validator {validator!r}")
        action = definition.get("action", "")
        if action and action not in _ACTIONS:
            raise ValueError(f"DLP rule {name!r} uses unknown action {action!r}")
        placeholder = definition.get("placeholder", "")
        if placeholder and (not isinstance(placeholder, str) or "{rule}" not in placeholder):
            raise ValueError(f"DLP rule {name!r} placeholder must contain {{rule}}")
        min_entropy = float(definition.get("min_entropy", 0))
        max_matches = int(definition.get("max_matches", 100))
        secret_group = int(definition.get("secret_group", 0))
        enabled = definition.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"DLP rule {name!r} enabled must be boolean")
        if min_entropy < 0 or max_matches <= 0 or secret_group < 0:
            raise ValueError(f"DLP rule {name!r} has invalid limits")
        compiled = re.compile(pattern_text, flags) if pattern_text else None
        if compiled is not None and secret_group > compiled.groups:
            raise ValueError(f"DLP rule {name!r} secret_group does not exist")
        rules[name] = DlpRule(
            name=name,
            pattern=compiled,
            validator=validator,
            keywords=tuple(word.lower() for word in _string_list(definition.get("keywords"), "keywords", name)),
            min_entropy=min_entropy,
            action=action,
            placeholder=placeholder,
            allowlist=tuple(word.lower() for word in _string_list(definition.get("allowlist"), "allowlist", name)),
            max_matches=max_matches,
            secret_group=secret_group,
            enabled=enabled,
            json_keys=json_keys,
        )

    if raw.get("version") == 1:
        legacy_keys = _string_list(raw.get("sensitive_json_keys"), "sensitive_json_keys")
        if legacy_keys:
            rules["structured_secret"] = DlpRule("structured_secret", json_keys=frozenset(k.lower() for k in legacy_keys))
    return DlpPolicy(raw["version"], rules, default_action, default_placeholder)


def _valid_id_card(value):
    expected = _ID_CHECK[sum(int(n) * w for n, w in zip(value[:17], _ID_WEIGHTS)) % 11]
    return value[-1].upper() == expected


def _valid_bank_card(value):
    digits = "".join(char for char in value if char.isdigit())
    if len(digits) < 13 or len(digits) > 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        number = int(char)
        if index % 2 == parity:
            number = number * 2 - 9 if number > 4 else number * 2
        total += number
    return total % 10 == 0


def _entropy(value):
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _rule_action(rule, mode, policy):
    return rule.action or (mode if mode in _ACTIONS else policy.default_action)


def _candidate_allowed(candidate, rule):
    lowered = candidate.lower()
    return any(item in lowered for item in rule.allowlist)


def _candidate_valid(candidate, rule):
    if _candidate_allowed(candidate, rule) or _entropy(candidate) < rule.min_entropy:
        return False
    if rule.validator == "cn_id_checksum":
        return _valid_id_card(candidate)
    if rule.validator == "luhn":
        return _valid_bank_card(candidate)
    return True


def _known_secret_pattern(known_secrets, min_length):
    secrets = sorted(
        {secret for secret in known_secrets if isinstance(secret, str) and len(secret) >= min_length},
        key=len, reverse=True,
    )
    return re.compile("|".join(re.escape(secret) for secret in secrets)) if secrets else None


def _decoded_text(raw):
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not value or not all(char.isprintable() or char in "\r\n\t" for char in value):
        return None
    return value


def _decode_candidates(value, budget):
    for kind, pattern in (
        ("hex", _HEX_CANDIDATE),
        ("percent", _PERCENT_CANDIDATE),
        ("base64", _BASE64_CANDIDATE),
    ):
        for match in pattern.finditer(value):
            if kind == "percent" and "%" not in match.group(0):
                continue
            if budget.bytes <= 0:
                budget.exhausted = True
                return
            start, end, candidate = match.start(), match.end(), match.group(0)
            if kind == "base64" and _HEX_CANDIDATE.fullmatch(candidate):
                continue
            # Reject oversized candidates before allocating their decoded representation.
            if len(candidate) > budget.bytes * 3 + 8:
                budget.exhausted = True
                return
            try:
                if kind == "base64":
                    if len(candidate) % 4 == 1:
                        continue
                    padded = candidate + "=" * (-len(candidate) % 4)
                    raw = base64.b64decode(padded, altchars=b"-_", validate=True)
                elif kind == "hex":
                    raw = bytes.fromhex(candidate)
                else:
                    raw = unquote_to_bytes(candidate)
            except (binascii.Error, ValueError):
                continue
            if not raw:
                continue
            if len(raw) > budget.bytes:
                budget.exhausted = True
                return
            budget.bytes -= len(raw)
            decoded = _decoded_text(raw)
            if decoded is None or decoded == candidate:
                continue
            if budget.candidates <= 0:
                budget.exhausted = True
                return
            budget.candidates -= 1
            yield start, end, decoded


def _collect_spans(value, enabled_rules, mode, policy, known_secret_pattern,
                   decode_depth, decode_budget):
    spans = []
    matched = set()
    blocked = set()
    audited = set()
    lowered = value.lower()
    for name in enabled_rules & policy.rules.keys():
        rule = policy.rules[name]
        if not rule.enabled or rule.pattern is None:
            continue
        if rule.keywords and not any(keyword in lowered for keyword in rule.keywords):
            continue
        count = 0
        for match in rule.pattern.finditer(value):
            candidate = match.group(rule.secret_group)
            if not _candidate_valid(candidate, rule):
                continue
            action = _rule_action(rule, mode, policy)
            spans.append((match.start(rule.secret_group), match.end(rule.secret_group), name, action, rule))
            matched.add(name)
            if action == "block": blocked.add(name)
            if action == "audit": audited.add(name)
            count += 1
            if count >= rule.max_matches:
                break

    known_action = mode if mode in _ACTIONS else policy.default_action
    known_rule = DlpRule("known_secret")
    if known_secret_pattern is not None:
        for count, match in enumerate(known_secret_pattern.finditer(value)):
            start, end = match.span()
            spans.append((start, end, "known_secret", known_action, known_rule))
            matched.add("known_secret")
            if known_action == "block":
                blocked.add("known_secret")
            if known_action == "audit":
                audited.add("known_secret")
            if count >= 99:
                break

    if decode_depth > 0:
        encoded_rule = DlpRule("encoded_secret")
        for start, end, decoded in _decode_candidates(value, decode_budget):
            nested_spans, nested_matched, nested_blocked, nested_audited = _collect_spans(
                decoded, enabled_rules, mode, policy, known_secret_pattern, decode_depth - 1,
                decode_budget,
            )
            if not nested_spans:
                continue
            action = max((span[3] for span in nested_spans), key=_ACTION_PRIORITY.__getitem__)
            spans.append((start, end, "encoded_secret", action, encoded_rule))
            matched.update(nested_matched)
            matched.add("encoded_secret")
            blocked.update(nested_blocked)
            audited.update(nested_audited)
            if action == "block":
                blocked.add("encoded_secret")
            if action == "audit":
                audited.add("encoded_secret")
    return spans, matched, blocked, audited


def _inspect_text(value, enabled_rules, mode, policy, known_secret_pattern,
                  decode_depth, decode_budget):
    spans, matched, blocked, audited = _collect_spans(
        value, enabled_rules, mode, policy, known_secret_pattern, decode_depth, decode_budget,
    )
    if not spans:
        return value, matched, 0, blocked, audited

    selected = []
    occupied = []
    for span in sorted(spans, key=lambda item: (item[0], -_ACTION_PRIORITY[item[3]], -(item[1] - item[0]))):
        start, end = span[0], span[1]
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        selected.append(span)
        occupied.append((start, end))
    transforms = sorted((span for span in selected if span[3] == "redact"), key=lambda item: item[0])
    if not transforms:
        return value, matched, 0, blocked, audited
    output = []
    position = 0
    for start, end, name, _action, rule in transforms:
        output.append(value[position:start])
        template = rule.placeholder or policy.default_placeholder
        output.append(template.format(rule=name))
        position = end
    output.append(value[position:])
    return "".join(output), matched, len(transforms), blocked, audited


def _process_text(value, start_marker, end_marker, strip_markers, enabled_rules, mode, policy,
                  allow_exemptions, known_secret_pattern, decode_depth, decode_budget):
    output = []
    matched, blocked, audited = set(), set(), set()
    exemptions = redactions = position = 0

    def inspect(segment):
        nonlocal redactions
        cleaned, found, count, denied, observed = _inspect_text(
            segment, enabled_rules, mode, policy, known_secret_pattern, decode_depth,
            decode_budget,
        )
        matched.update(found); blocked.update(denied); audited.update(observed); redactions += count
        return cleaned

    if not allow_exemptions:
        return inspect(value), matched, 0, redactions, blocked, audited

    while position < len(value):
        start = value.find(start_marker, position)
        if start < 0:
            output.append(inspect(value[position:])); break
        output.append(inspect(value[position:start]))
        end = value.find(end_marker, start + len(start_marker))
        if end < 0:
            output.append(inspect(value[start:])); break
        content = value[start + len(start_marker):end]
        if start_marker in content:
            output.append(inspect(value[start:end + len(end_marker)]))
            position = end + len(end_marker); continue
        exemptions += 1
        output.append(content if strip_markers else start_marker + content + end_marker)
        position = end + len(end_marker)
    return "".join(output), matched, exemptions, redactions, blocked, audited


def inspect_json_body(body, enabled_rules, start_marker, end_marker, strip_markers=True,
                      mode="redact", rule_file="", redact=None, allow_exemptions=True,
                      decode_depth=0, decode_max_candidates=100, decode_max_bytes=1048576,
                      known_secrets=(), known_secret_min_length=8):
    if not body:
        return DlpResult(body, (), 0)
    if redact is not None:
        mode = "redact" if redact else "audit"
    if allow_exemptions and (not start_marker or not end_marker or start_marker == end_marker):
        return DlpResult(body, (), 0, True)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return DlpResult(body, (), 0, uninspectable=True)

    policy = load_policy(rule_file)
    decode_budget = _DecodeBudget(max(0, decode_max_candidates), max(0, decode_max_bytes))
    known_secret_pattern = _known_secret_pattern(tuple(known_secrets), known_secret_min_length)
    matched, blocked, audited = set(), set(), set()
    exemptions = redactions = 0

    def visit(value):
        nonlocal exemptions, redactions
        if isinstance(value, str):
            cleaned, found, exempted, replaced, denied, observed = _process_text(
                value, start_marker, end_marker, strip_markers, enabled_rules, mode, policy,
                allow_exemptions, known_secret_pattern, decode_depth, decode_budget,
            )
            matched.update(found); blocked.update(denied); audited.update(observed)
            exemptions += exempted; redactions += replaced
            return cleaned
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            output = {}
            structured = policy.rules.get("structured_secret")
            for key, item in value.items():
                if (key.lower() in _BINARY_KEYS and isinstance(item, str)
                        and (item.startswith("data:") or len(item) > 4096 and _BASE64.fullmatch(item))):
                    output[key] = item
                    continue
                cleaned = visit(item)
                if (structured and "structured_secret" in enabled_rules and structured.enabled
                        and key.lower() in structured.json_keys and isinstance(item, str) and item
                        and _candidate_valid(item, structured)):
                    stripped = item.strip()
                    if not (stripped.startswith(start_marker) and stripped.endswith(end_marker)):
                        action = _rule_action(structured, mode, policy)
                        matched.add("structured_secret")
                        if action == "block": blocked.add("structured_secret")
                        elif action == "audit": audited.add("structured_secret")
                        elif "[REDACTED:" not in cleaned:
                            template = structured.placeholder or policy.default_placeholder
                            cleaned = template.format(rule="structured_secret"); redactions += 1
                output[key] = cleaned
            return output
        return value

    def visit_sensitive_items(items):
        output = list(items)
        indexes = [index for index, item in enumerate(items) if isinstance(item, dict) and (
            item.get("role") in ("user", "tool") or item.get("type") in (
                "function_call_output", "computer_call_output", "local_shell_call_output", "mcp_call_output"))]
        if indexes:
            for index in indexes: output[index] = visit(items[index])
        else:
            output = [visit(item) if isinstance(item, str) else item for item in items]
        return output

    if isinstance(payload, dict):
        cleaned = dict(payload); recognized = False
        if isinstance(payload.get("messages"), list):
            cleaned["messages"] = visit_sensitive_items(payload["messages"]); recognized = True
        if "input" in payload:
            value = payload["input"]
            cleaned["input"] = visit_sensitive_items(value) if isinstance(value, list) else visit(value)
            recognized = True
        for key in ("prompt", "query"):
            if isinstance(payload.get(key), str): cleaned[key] = visit(payload[key]); recognized = True
        if not recognized: cleaned = visit(payload)
    else:
        cleaned = visit(payload)
    encoded = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return DlpResult(
        encoded, tuple(sorted(matched)), exemptions, False, redactions,
        tuple(sorted(blocked)), tuple(sorted(audited)),
        limit_exceeded=decode_budget.exhausted,
    )


def validate_policy(path):
    policy = load_policy(path)
    return {"version": policy.version, "rules": len(policy.rules),
            "enabled": sum(1 for rule in policy.rules.values() if rule.enabled)}


def _main():
    import argparse

    parser = argparse.ArgumentParser(description="Validate an llm-retry-proxy DLP rule file")
    parser.add_argument("command", choices=("validate",))
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    if args.command == "validate":
        if args.path:
            path = args.path
        else:
            from .config import settings
            path = settings.dlp_rule_file
        result = validate_policy(path)
        print(json.dumps({"path": path, **result}, ensure_ascii=False))


if __name__ == "__main__":
    _main()
