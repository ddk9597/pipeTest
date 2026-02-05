import json
import os
import subprocess
import time
import requests

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MAX_ITERS = int(os.getenv("MAX_ITERS", "5"))

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

def sh(cmd: str) -> str:
    out = subprocess.check_output(cmd, shell=True, text=True)
    return out.strip()

def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def write_file(path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def call_claude(system_text: str, user_text: str, model: str = "claude-sonnet-4-5", max_tokens: int = 1600) -> str:
    headers = {
        "content-type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_text,
        "messages": [{"role": "user", "content": user_text}],
    }
    r = requests.post(ANTHROPIC_MESSAGES_URL, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    # content: [{"type":"text","text":"..."}]
    return "".join([c.get("text", "") for c in data.get("content", [])])

def call_openai_codex(input_text: str, model: str = "gpt-5.2-codex") -> str:
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {OPENAI_API_KEY}",
    }
    payload = {
        "model": model,
        "input": input_text,
    }
    r = requests.post(OPENAI_RESPONSES_URL, headers=headers, json=payload, timeout=180)
    r.raise_for_status()
    data = r.json()
    # Responses API output format may contain output_text in convenience fields in some SDKs;
    # here we defensively join text blocks.
    text_parts = []
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                text_parts.append(c.get("text", ""))
    return "\n".join(text_parts).strip()

def apply_patch(diff_text: str) -> bool:
    write_file("patch.diff", diff_text)
    # git apply returns non-zero if patch fails
    p = subprocess.run("git apply --whitespace=nowarn patch.diff", shell=True, text=True)
    return p.returncode == 0

def main():
    pr_req = read_file("input.md")

    # 작업 컨텍스트(리포지토리 상태 요약)
    repo_status = sh("git status --porcelain") or "(clean)"
    repo_head = sh("git rev-parse --short HEAD")
    context = f"Repo HEAD: {repo_head}\nRepo status: {repo_status}\n"

    # 1) Claude 설계
    plan_system = (
        "You are a senior software planner. "
        "Write a concrete implementation plan in Markdown with steps and acceptance criteria. "
        "Be concise and actionable."
    )
    plan_user = f"{context}\n\nPR Requirements:\n{pr_req}"
    plan_md = call_claude(plan_system, plan_user, max_tokens=1400)
    write_file("plan.md", plan_md)

    # 루프
    for i in range(1, MAX_ITERS + 1):
        # 2) Codex 실제 작업: diff만 출력 강제
        codex_prompt = f"""
You are an implementation agent. Follow the plan and modify the repository.
Return ONLY a unified diff (git patch). Do not include explanations.

Plan (Markdown):
{plan_md}

PR Requirements:
{pr_req}

Constraints:
- Produce a patch that applies cleanly with `git apply`.
- Keep changes minimal and relevant.
"""
        diff = call_openai_codex(codex_prompt)
        if not diff.startswith("diff --git"):
            # 방어: diff가 아니면 실패 처리
            write_file("verify.json", json.dumps({"verdict": "NONPASS", "reason": "Codex did not return a unified diff"}, ensure_ascii=False, indent=2))
            continue

        # patch 적용
        # (반복 시 이전 변경을 되돌리고 다시 적용하고 싶으면 reset하는 방식도 가능)
        sh("git reset --hard")
        ok = apply_patch(diff)
        if not ok:
            write_file("verify.json", json.dumps({"verdict": "NONPASS", "reason": "Patch failed to apply"}, ensure_ascii=False, indent=2))
            continue

        # 3) Claude 검증: PASS/NONPASS JSON으로만
        # 검증용 컨텍스트: 변경된 파일 목록 + diff
        changed = sh("git diff --name-only") or "(none)"
        current_diff = sh("git diff")
        verify_system = (
            "You are a strict reviewer. "
            "Return ONLY JSON with fields: verdict (PASS or NONPASS), reasons (array of strings), "
            "and if NONPASS, required_changes (array of specific actionable items)."
        )
        verify_user = f"""
PR Requirements:
{pr_req}

Plan:
{plan_md}

Changed files:
{changed}

Diff:
{current_diff}
"""
        verify_text = call_claude(verify_system, verify_user, max_tokens=1600)
        # Claude가 JSON 이외를 섞을 수 있으니 최대한 파싱 시도
        try:
            start = verify_text.find("{")
            end = verify_text.rfind("}")
            verify_json = json.loads(verify_text[start:end+1])
        except Exception:
            verify_json = {"verdict": "NONPASS", "reasons": ["Reviewer output was not valid JSON"], "required_changes": []}

        write_file("verify.json", json.dumps(verify_json, ensure_ascii=False, indent=2))

        if verify_json.get("verdict") == "PASS":
            # 최종 보고서(md)
            report = f"""# AI Staff Report

## Verdict
PASS (iteration {i})

## Plan
{plan_md}

## Summary of changes
Changed files:
{changed}

## Notes
- Patch saved to `patch.diff`
- Reviewer details saved to `verify.json`
"""
            write_file("report.md", report)
            return

        # NONPASS면 다음 반복을 위해 plan_md에 피드백을 반영하거나,
        # codex_prompt에 required_changes를 추가하는 방식으로 강화
        required = verify_json.get("required_changes", [])
        plan_md_updated = plan_md + "\n\n---\n\n## Reviewer required changes\n" + "\n".join([f"- {x}" for x in required])
        plan_md = plan_md_updated
        write_file("plan.md", plan_md)

        time.sleep(1)

    # MAX_ITERS 초과: 실패 리포트
    report = f"""# AI Staff Report

## Verdict
NONPASS (exhausted {MAX_ITERS} iterations)

## Last Plan
{plan_md}

## Reviewer details
See `verify.json`.
"""
    write_file("report.md", report)

if __name__ == "__main__":
    main()
