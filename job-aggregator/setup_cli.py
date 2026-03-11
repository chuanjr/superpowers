#!/usr/bin/env python3
"""Interactive CLI to configure job-aggregator."""
import sys
from pathlib import Path
import yaml

CONFIG_PATH = Path("config.yaml")


def prompt(question: str, default: str = "") -> str:
    display = f"{question} [{default}]: " if default else f"{question}: "
    answer = input(display).strip()
    return answer if answer else default


def prompt_list(question: str, default: list[str]) -> list[str]:
    print(f"{question} (comma-separated) [{', '.join(default)}]: ", end="")
    answer = input().strip()
    if not answer:
        return default
    return [item.strip() for item in answer.split(",") if item.strip()]


def prompt_bool(question: str, default: bool) -> bool:
    default_str = "Y/n" if default else "y/N"
    answer = input(f"{question} [{default_str}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def run_setup():
    print("\n=== Job Aggregator Setup ===\n")

    existing = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            existing = yaml.safe_load(f) or {}
        print("Updating existing config. Press Enter to keep current values.\n")

    # Markets
    markets = prompt_list(
        "Markets to search (tw/jp/sg)",
        existing.get("markets", ["tw", "jp", "sg"])
    )

    # Target roles
    targets = existing.get("targets", {})
    titles = prompt_list(
        "Target job titles",
        targets.get("titles", ["Backend Engineer", "Software Engineer"])
    )
    experience = prompt("Experience years (e.g. 3-5)", targets.get("experience_years", "3-5"))
    excludes = prompt_list(
        "Exclude keywords",
        targets.get("exclude_keywords", ["outsourcing", "派遣"])
    )

    # Sources
    src = existing.get("sources", {})
    sources = {
        "linkedin_gmail": prompt_bool("Enable LinkedIn (Gmail)", src.get("linkedin_gmail", True)),
        "indeed_gmail": prompt_bool("Enable Indeed (Gmail)", src.get("indeed_gmail", True)),
        "indeed_rss": prompt_bool("Enable Indeed (RSS)", src.get("indeed_rss", True)),
        "104": prompt_bool("Enable 104", src.get("104", True)),
        "cakeresume": prompt_bool("Enable CakeResume", src.get("cakeresume", True)),
        "yourator": prompt_bool("Enable Yourator", src.get("yourator", True)),
        "wellfound": prompt_bool("Enable Wellfound", src.get("wellfound", True)),
    }

    # Notification
    notif = existing.get("notification", {})
    email_to = prompt("Send digest to (email)", notif.get("to", ""))
    email_from = prompt("Send digest from (email)", notif.get("from", email_to))

    days_back = int(prompt("Days back to search (e.g. 7)", str(existing.get("days_back", 7))))

    config = {
        "markets": markets,
        "targets": {
            "titles": titles,
            "experience_years": experience,
            "exclude_keywords": excludes,
        },
        "sources": sources,
        "notification": {"to": email_to, "from": email_from},
        "days_back": days_back,
    }

    CONFIG_PATH.write_text(yaml.dump(config, allow_unicode=True, default_flow_style=False))
    print(f"\nConfig saved to {CONFIG_PATH}")
    print("\nNext: place your Google OAuth client_secret.json in credentials/")
    print("Then run: python main.py\n")


if __name__ == "__main__":
    run_setup()
