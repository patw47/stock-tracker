from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "deploy_verdict.py"
_spec = importlib.util.spec_from_file_location("deploy_verdict", _MODULE_PATH)
verdict = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
sys.modules["deploy_verdict"] = verdict
_spec.loader.exec_module(verdict)


def _all_active() -> dict[str, str]:
    statuses = dict.fromkeys(verdict._REQUIRED_ACTIVE, "active")
    statuses["STATUS_N8N_HEALTH"] = "ok"
    return statuses


def _old_overall(st: str, n8n: str, warren: str, import_rc: int, registry_rc: int) -> str:
    """Ancienne condition de remote.sh (avant Epic 9 S2) : ignorait les 4
    timers systemd — reproduite ici pour prouver mécaniquement qu'un timer
    mort ne changeait rien à son verdict.
    """
    if st != "active" or n8n != "ok" or warren != "active" or import_rc != 0 or registry_rc != 0:
        return "fail"
    return "ok"


def test_nominal_all_active_is_ok() -> None:
    assert verdict.compute_overall(_all_active(), import_rc=0, registry_rc=0) == "ok"


@pytest.mark.parametrize(
    "inactive_key",
    [
        "STATUS_WATCHDOG_TIMER",
        "STATUS_OUTCOME_TIMER",
        "STATUS_TENSION_TIMER",
        "STATUS_V5_TIMER",
    ],
)
def test_single_timer_inactive_was_ok_before_is_fail_after(inactive_key: str) -> None:
    # Rouge avant le sprint : l'ancienne condition ne voyait pas les timers,
    # donc un seul inactif ne la faisait jamais échouer.
    assert _old_overall("active", "ok", "active", 0, 0) == "ok"

    # Vert après le sprint : le nouveau calcul les inclut tous.
    statuses = _all_active()
    statuses[inactive_key] = "inactive"
    assert verdict.compute_overall(statuses, import_rc=0, registry_rc=0) == "fail"


def test_n8n_health_not_ok_is_fail() -> None:
    statuses = _all_active()
    statuses["STATUS_N8N_HEALTH"] = "fail"
    assert verdict.compute_overall(statuses, import_rc=0, registry_rc=0) == "fail"


def test_import_rc_nonzero_is_fail() -> None:
    assert verdict.compute_overall(_all_active(), import_rc=1, registry_rc=0) == "fail"


def test_registry_rc_nonzero_is_fail() -> None:
    assert verdict.compute_overall(_all_active(), import_rc=0, registry_rc=1) == "fail"
