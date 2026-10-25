"""Run with: uv run python examples/adversarial_testing.py --case-out counterexample.json"""

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path

from aegentra import Knowledge
from aegentra.adversary import (
    Action,
    FaultSearchAgent,
    Target,
    TrialResult,
    run_trial,
    save_case,
)


def show_trial(title: str, result: TrialResult) -> None:
    print(f"\n{title}")
    print(f"{'step':>4}  {'action':<16} {'client knowledge':<18} {'refunds':>7} {'pending':>7}")
    for event in result.trace:
        print(
            f"{event.step:>4}  {event.action:<16} {event.knowledge.value:<18} "
            f"{event.actual_effects:>7} {event.pending_requests:>7}"
        )
    findings = ", ".join(item.invariant.value for item in result.violations) or "none"
    print(f"Violations: {findings}; blocked proposals: {result.blocked_proposals}")
    print(f"Valid completion: {result.evaluation.valid_completion}")


async def demonstrate(case_out: Path | None) -> None:
    print("Aegentra testing agent: model-free search, independent provider oracle.")
    print("Start with an acknowledged request and benign observation/time actions.")
    report = await FaultSearchAgent().search()
    if report.failure is None or report.reduction is None:
        raise RuntimeError("The demo search did not find the expected unsafe-client failure.")

    found, reduced = report.failure, report.reduction
    print(
        f"\nExplored {len(report.attempts)} fresh trials; "
        f"retained {report.signatures} behavioral transition signatures."
    )
    print(f"Discovered scenario: {found.case.scenario.value}")
    print("Found actions:", " -> ".join(action.value for action in found.case.actions))
    print(
        f"Reduced {len(found.case.actions)} actions to {len(reduced.result.case.actions)} "
        f"using {reduced.trials} fresh probes; single-deletion minimal: {reduced.one_minimal}."
    )
    print("Reduced actions:", " -> ".join(action.value for action in reduced.result.case.actions))
    if case_out is not None:
        save_case(reduced.result.case, case_out)
        print(f"Replay artifact: {case_out}")

    show_trial("Unsafe client: minimized replay", reduced.result)
    defended = await run_trial(reduced.result.case, target=Target.AEGENTRA)
    show_trial("Aegentra: exactly the same case", defended)
    if defended.violations:
        raise RuntimeError("The same case violated an invariant behind Aegentra's boundary.")

    if defended.trace[-1].knowledge is Knowledge.UNKNOWN:
        print("\nSafety is not completion: the matched replay is still unresolved.")
        print("Separate recovery trace: append advance -> observe, then replay from fresh state.")
        extended = replace(
            reduced.result.case,
            actions=reduced.result.case.actions + (Action.ADVANCE, Action.OBSERVE),
        )
        recovered = await run_trial(extended, target=Target.AEGENTRA)
        show_trial("Aegentra: extended recovery trace (different input)", recovered)
        if recovered.violations:
            raise RuntimeError("The extended recovery trace violated an invariant.")
    print("\nThese are bounded sandbox counterexamples, not a proof of universal safety.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-out", type=Path, help="save the discovered case in a new file")
    arguments = parser.parse_args()
    try:
        asyncio.run(demonstrate(arguments.case_out))
    except OSError as exc:
        parser.exit(1, f"demo: error: {exc}\n")


if __name__ == "__main__":
    main()
