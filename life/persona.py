"""Versioned persona compilation. Explicit settings win; estimates remain visible."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tomllib
import tempfile
from typing import Any

from .contracts import clamp, clamp01, now_ts
from .drives import DRIVE_DIMENSIONS, DRIVE_LABELS, canonical_dimension

DIMENSIONS = ("傲慢", "嫉妒", "愤怒", "懒惰", "贪婪", "暴食", "色欲",
              "谦逊", "宽厚", "温和", "勤勉", "慷慨", "节制", "忠贞")
COMPILER_VERSION = "persona-4-explicit"
AUDIENCE_BLOCK = re.compile(r'<!-- persona:(public|private) -->\s*\n?(.*?)<!-- /persona:\1 -->',re.S)


def write_view(path,body):
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,name = tempfile.mkstemp(prefix='.persona-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8',newline='\n') as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def ensure_audience_sections(path,legacy_public):
    """One-time conservative partition of a pre-existing private persona."""
    source = path.read_text('utf-8')
    if not AUDIENCE_BLOCK.search(source) and legacy_public.is_file():
        common = legacy_public.read_text('utf-8')
        if common.startswith('<!-- Derived persona'):
            return
        write_view(path,'<!-- persona:public -->\n'+common+'\n<!-- /persona:public -->\n\n'
            '<!-- persona:private -->\n'+source+'\n<!-- /persona:private -->\n')


def public_persona(path,derived=None):
    """Unmarked/common text and public sections grow together; private blocks stay private."""
    source = path.read_text('utf-8') if path.is_file() else ''
    text = project_public_text(source)
    if derived is not None:
        body = '<!-- Derived persona sha256='+hashlib.sha256(source.encode()).hexdigest()+' -->\n'+text+'\n'
        if not derived.is_file() or derived.read_text('utf-8')!=body:
            write_view(derived,body)
    return text


def project_public_text(source):
    text = AUDIENCE_BLOCK.sub(lambda m:m[2] if m[1]=='public' else '',source).strip()
    if '<!-- persona:' in text or '<!-- /persona:' in text:
        raise ValueError('malformed_persona_audience_block')
    return text


def replace_public_persona(source,text):
    if AUDIENCE_BLOCK.search(source):
        private = '\n'.join(m[0] for m in AUDIENCE_BLOCK.finditer(source) if m[1]=='private')
        return '<!-- persona:public -->\n'+text+'\n<!-- /persona:public -->\n'+private+'\n'
    return text


def local_parameters(source):
    """Read explicit numeric/qualitative declarations, preserving exact quotes.

    Ambiguous prose remains unresolved; no source text leaves the process.
    """
    found = {}
    levels = {"极低": .1, "很低": .2, "偏低": .35, "中等": .5,
              "偏高": .65, "很高": .8, "极高": .9}
    for dimension in DIMENSIONS:
        declarations = []
        pattern = re.compile(re.escape(dimension) + r"\s*[:：=]\s*(\d+(?:\.\d+)?)\s*(/\s*10|%|/\s*1(?!\d))?")
        for match in pattern.finditer(source):
            value = float(match[1])
            scale = (match[2] or "").replace(" ", "")
            value = value / (100 if scale == "%" else 10 if scale == "/10" else 1)
            if 0 <= value <= 1:
                declarations.append((value, match.group()))
        for word, value in levels.items():
            match = re.search(re.escape(dimension) + r"\s*[:：=]\s*" + word, source)
            if match:
                declarations.append((value, match.group()))
        if declarations and len({value for value, _quote in declarations}) == 1:
            value, quote = declarations[0]
            found[dimension] = {"value": value, "source_quote": quote, "method": "local_explicit"}
    return found


@dataclass
class PersonaSpec:
    version: str
    digest: str
    source_path: str
    text: str
    identity: str
    desires: dict[str, float]
    provenance: dict[str, Any]
    interests: list[str] = field(default_factory=list)
    compiler_version: str = COMPILER_VERSION
    unresolved_fields: list[str] = field(default_factory=list)
    schema_version: int = 3
    # drive_v2：稳定人格只提供参数与选择权重，不把旧十四维当当前需要。
    drive_targets: dict[str, float] = field(default_factory=dict)
    drive_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    autonomy_weight: float = 0.50
    contact_preference: float = 0.50




def local_drive_parameters(source: str) -> dict[str, dict[str, Any]]:
    """Local, traceable drive preference extraction.

    只读取明确的数值或“维度：程度”声明并保留原文，不按词频推断。
    自由描述的意义由 DSH 主角色理解；未明确量化的字段使用可见默认值。
    """
    found: dict[str, dict[str, Any]] = {}
    levels = {"极低": .10, "很低": .20, "偏低": .35, "中等": .50,
              "偏高": .65, "很高": .80, "极高": .90}
    lines = source.splitlines()
    for dim in DRIVE_DIMENSIONS:
        label = DRIVE_LABELS[dim]
        numeric = re.compile(re.escape(label) + r"\s*[:：=]\s*(\d+(?:\.\d+)?)\s*(/\s*10|%|/\s*1(?!\d))?")
        for match in numeric.finditer(source):
            value = float(match[1])
            scale = (match[2] or "").replace(" ", "")
            value = value / (100 if scale == "%" else 10 if scale == "/10" else 1)
            if 0 <= value <= 1:
                found[dim] = {"value": round(value, 4), "source_quote": match.group(),
                              "method": "local_explicit"}
                break
        if dim in found:
            continue
        for word, value in levels.items():
            match = re.search(re.escape(label) + r"\s*[:：=]\s*" + word, source)
            if match:
                found[dim] = {"value": value, "source_quote": match.group(),
                              "method": "local_explicit"}
                break
        if dim in found:
            continue
    return found


def compile_persona(path: Path, *, extractor=None, source_text=None) -> PersonaSpec:
    source = path.read_text(encoding="utf-8") if source_text is None else source_text
    # Validate declared audiences before a candidate can become the current persona.
    project_public_text(source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    structured = {}
    for block in re.findall(r"```toml\s*\n(.*?)```", source, re.S):
        candidate = tomllib.loads(block)
        if "persona" in candidate:
            structured = candidate["persona"]
            break
    proposals = extractor(source) if extractor is not None and not structured else local_parameters(source)
    if proposals:
        for value in proposals.values():
            if not isinstance(value, dict) or value.get("source_quote", "") not in source or not value.get("source_quote"):
                raise ValueError("persona_candidate_without_provenance")
    heading = re.search(r"^#\s+(.+)$", source, re.M)
    identity = str(structured.get("identity") or (heading.group(1) if heading else path.stem))
    values, provenance, unresolved = {}, {"identity": {"source": "explicit" if structured.get("identity") else "heading"}}, []
    for dimension in DIMENSIONS:
        value = structured.get("desires", {}).get(dimension)
        proposed = proposals.get(dimension) if proposals else None
        if value is None and proposed:
            value = proposed.get("value")
        if value is None:
            values[dimension] = 0.5
            provenance[dimension] = {"source": "visible_default", "value": 0.5}
            unresolved.append(dimension)
        else:
            number = float(value)
            if not math.isfinite(number) or not 0 <= number <= 1:
                raise ValueError("persona_desire_out_of_range")
            values[dimension] = number
            provenance[dimension] = ({"source": "explicit_toml"} if not proposed else
                                     {"source": proposed.get("method", "interpretation"), "quote": proposed["source_quote"]})
    interests = [str(x)[:100] for x in structured.get("interests", [])]
    if not interests:
        section = re.search(r"^#{1,6}\s*(?:兴趣|爱好|兴趣爱好)\s*\n(.*?)(?=^#{1,6}\s|\Z)", source, re.S | re.M)
        if section:
            interests = [line.lstrip("-* ").strip()[:100] for line in section[1].splitlines() if line.strip()][:8]

    # ---- drive_v2：人格只提供稳定偏好与参数覆盖，不生成当前 tension ----
    drive_targets: dict[str, float] = {}
    drive_overrides: dict[str, dict[str, Any]] = {}
    drive_proposals = local_drive_parameters(source)
    raw_targets = structured.get("drive_targets") or {}
    raw_drives = structured.get("drives") or {}
    for dim in DRIVE_DIMENSIONS:
        raw_drive = raw_drives.get(dim) if isinstance(raw_drives, dict) else None
        value = None
        source_meta: dict[str, Any] = {}
        if isinstance(raw_targets, dict) and raw_targets.get(dim) is not None:
            value = raw_targets.get(dim)
            source_meta = {"source": "explicit_toml"}
        elif isinstance(raw_drive, (int, float)):
            value = raw_drive
            source_meta = {"source": "explicit_toml"}
        elif isinstance(raw_drive, dict) and raw_drive.get("preference") is not None:
            value = raw_drive.get("preference")
            source_meta = {"source": "explicit_toml"}
        elif dim in drive_proposals:
            value = drive_proposals[dim].get("value")
            source_meta = {"source": drive_proposals[dim].get("method", "local_interpretation"),
                           "quote": drive_proposals[dim].get("source_quote", "")}
        if value is None:
            drive_targets[dim] = 0.50
            provenance["drive:" + dim] = {"source": "visible_default", "value": 0.50}
            unresolved.append("drive:" + dim)
        else:
            number = float(value)
            if not math.isfinite(number) or not 0 <= number <= 1:
                raise ValueError("persona_drive_out_of_range")
            drive_targets[dim] = number
            source_meta = dict(source_meta)
            source_meta["value"] = number
            provenance["drive:" + dim] = source_meta
        if isinstance(raw_drive, dict):
            allowed = {
                "mode", "initial_tension", "reference_tension", "ceiling",
                "base_growth_per_hour", "growth_multiplier_min", "growth_multiplier_max",
                "expression_threshold", "high_threshold", "high_dwell_hours",
                "interaction_satisfaction_gain", "regulation_relief_gain",
                "regulation_enabled", "recovery_hours", "recovery_growth_multiplier",
                "contact_cooldown_hours", "max_contact_attempts_per_window",
            }
            drive_overrides[dim] = {str(k): v for k, v in raw_drive.items()
                                    if k in allowed and k != "preference"}
    try:
        autonomy_weight = clamp01(float(structured.get("autonomy_weight", 0.50)))
    except (TypeError, ValueError):
        autonomy_weight = 0.50
    try:
        contact_preference = clamp01(float(structured.get("contact_preference", 0.50)))
    except (TypeError, ValueError):
        contact_preference = 0.50
    return PersonaSpec(
        "persona:" + digest[:24], digest, str(path.resolve()), source,
        identity, values, provenance, interests,
        unresolved_fields=unresolved, schema_version=3,
        drive_targets=drive_targets, drive_overrides=drive_overrides,
        autonomy_weight=autonomy_weight, contact_preference=contact_preference,
    )

class PersonaService:
    def __init__(self, repo, agent_id):
        self.repo, self.agent_id = repo, agent_id

    def activate(self, path: Path, *, extractor=None, source_text=None) -> PersonaSpec:
        source = path.read_text(encoding='utf-8') if source_text is None else source_text
        digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
        cached = self.repo._one("SELECT spec FROM persona_compilations WHERE agent_id=? AND digest=? AND compiler_version=?",
                                (self.agent_id, digest, COMPILER_VERSION))
        spec = PersonaSpec(**json.loads(cached["spec"])) if cached else compile_persona(path, extractor=extractor, source_text=source)
        spec.version = "persona:" + hashlib.sha256(
            (self.agent_id + "\0" + spec.digest + "\0" + COMPILER_VERSION).encode()).hexdigest()[:32]
        with self.repo.tx():
            old = self.current()
            self.repo._exec("INSERT OR IGNORE INTO persona_compilations VALUES(?,?,?,?,?,?)",
                            (spec.version, self.agent_id, spec.digest, COMPILER_VERSION,
                             json.dumps(asdict(spec), ensure_ascii=False), now_ts()))
            self.repo._exec("INSERT OR REPLACE INTO profile_current VALUES(?,?)", (self.agent_id, spec.version))
            if old and old.version != spec.version:
                rows = self.repo._all("SELECT aggregate_id,version,payload FROM state_snapshots WHERE aggregate_id LIKE ?",
                                      (f"desire:{self.agent_id}|%",))
                latest = {}
                for row in rows:
                    if row["aggregate_id"] not in latest or latest[row["aggregate_id"]]["version"] < row["version"]:
                        latest[row["aggregate_id"]] = row
                for key, row in latest.items():
                    payload = json.loads(row["payload"])
                    payload["baseline"] = spec.desires
                    payload["profile_version"] = spec.version
                    self.repo.save_state(key, payload)
            return spec

    def text(self, path: Path) -> str:
        source = path.read_text(encoding='utf-8')
        digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
        current = self.current()
        if current is None or current.digest != digest:
            self.activate(path, source_text=source)
        return source

    def current(self) -> PersonaSpec | None:
        row = self.repo._one("SELECT p.spec FROM persona_compilations p JOIN profile_current c ON c.version_id=p.id WHERE c.agent_id=?",
                             (self.agent_id,))
        if row is None:
            row = self.repo._one("SELECT p.spec FROM profile_versions p JOIN profile_current c ON c.version_id=p.id WHERE c.agent_id=?", (self.agent_id,))
        return PersonaSpec(**json.loads(row["spec"])) if row else None
