"""Static checks for one-house GitHub workflow and secret boundaries."""
from pathlib import Path
import re
import subprocess

import yaml

ROOT=Path(__file__).resolve().parents[1]


def test_pinned_official_actions_and_checkout_has_no_persisted_credentials():
    for name in ("check.yml","tests.yml"):
        wf=yaml.safe_load((ROOT/".github/workflows"/name).read_text())
        job=next(iter(wf["jobs"].values()))
        actions=[step for step in job["steps"] if "uses" in step]
        assert actions
        assert all(re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}",s["uses"])
                   for s in actions)
        if name == "check.yml":
            assert actions[0]["with"]["persist-credentials"] is False


def test_read_step_does_not_get_state_push_token():
    wf=yaml.safe_load((ROOT/".github/workflows/check.yml").read_text())
    steps=wf["jobs"]["check"]["steps"]
    notify=next(s for s in steps if s.get("id")=="notify")
    save=next(s for s in steps if "Save notification state" in s.get("name",""))
    assert "GITHUB_STATE_PUSH_TOKEN" not in notify["env"]
    assert "GITHUB_STATE_PUSH_TOKEN" in save["env"]
    assert "TELEGRAM_BOT_TOKEN" not in save["env"]
    assert "DTEK_ADDRESS_JSON" not in save["env"]
    assert "persist-credentials: false" in (
        ROOT/".github/workflows/check.yml").read_text()
    assert "GIT_ASKPASS" in save["run"]
    assert 'git push "https://github.com/$GITHUB_REPOSITORY.git" HEAD:main' in save["run"]
    result=subprocess.run(["bash","-n"],input=save["run"],text=True,
                          capture_output=True,check=False)
    assert result.returncode == 0, result.stderr


def test_both_secrets_only_referenced_by_name():
    wf=(ROOT/".github/workflows/check.yml").read_text()
    assert "secrets.TELEGRAM_BOT_TOKEN" in wf
    assert "secrets.DTEK_ADDRESS_JSON" in wf
    for filename in ("bot.py","alerts.py","house_reliability.py"):
        content=(ROOT/filename).read_text(encoding="utf-8")
        assert "TELEGRAM_BOT_TOKEN" not in content or (
            'os.getenv("TELEGRAM_BOT_TOKEN"' in content)
        assert "https://api.telegram.org/bot" not in content or filename=="bot.py"


def test_single_house_does_not_implement_user_enumeration():
    content="\n".join(
        (ROOT/name).read_text(encoding="utf-8")
        for name in ("bot.py","alerts.py","house_reliability.py")
    )
    for forbidden in ("getChatAdministrators","getChatMemberCount",
                      "getUpdates","setWebhook","sendDocument","sendPhoto"):
        assert forbidden not in content
