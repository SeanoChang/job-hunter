"""Verification findings. Machine-verified is not true: this reports attribution
and internal consistency only (harness spec §3.4)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Finding:
    check: str
    path: str
    code: str
    detail: dict[str, object]
    severity: str  # "error" | "warning"


@dataclass
class Report:
    validator_version: str
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, object] = field(default_factory=dict)

    def error(self, check: str, path: str, code: str, **detail: object) -> None:
        self.findings.append(Finding(check, path, code, dict(detail), "error"))

    def warn(self, check: str, path: str, code: str, **detail: object) -> None:
        self.findings.append(Finding(check, path, code, dict(detail), "warning"))

    @property
    def status(self) -> str:
        return "fail" if any(f.severity == "error" for f in self.findings) else "pass"

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status,
            "validator_version": self.validator_version,
            "findings": [
                {
                    "check": f.check,
                    "path": f.path,
                    "code": f.code,
                    "severity": f.severity,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
            "metrics": self.metrics,
        }
