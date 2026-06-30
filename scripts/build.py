from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import requests
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config" / "sources.yaml"
DIST_DIR = ROOT / "dist"

VALID_BEHAVIORS = {"domain", "ipcidr", "classical"}


def log(message: str) -> None:
    print(message, flush=True)


def group(title: str) -> None:
    log(f"::group::{title}")


def end_group() -> None:
    log("::endgroup::")


def notice(message: str) -> None:
    log(f"::notice::{message}")


def error(message: str) -> None:
    log(f"::error::{message}")


def download(url: str, retries: int = 3, timeout: int = 60) -> str:
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                url,
                timeout=timeout,
                headers={
                    "User-Agent": "mihomo-mrs-builder/1.0",
                    "Accept": "text/plain, text/yaml, application/yaml, */*",
                },
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            log(f"Download failed, attempt {attempt}/{retries}: {url}")
            log(f"Reason: {exc}")
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"Failed to download {url}: {last_error}") from last_error


def clean_rule(value: Any) -> str | None:
    if value is None:
        return None

    rule = str(value).strip()

    if not rule:
        return None

    if rule.startswith("#"):
        return None

    if rule == "payload:":
        return None

    if rule.startswith("- "):
        rule = rule[2:].strip()

    rule = rule.strip("'\"").strip()

    if not rule:
        return None

    if rule.startswith("#"):
        return None

    return rule


def parse_yaml_payload(content: str) -> list[str] | None:
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return None

    if not isinstance(data, dict):
        return None

    payload = data.get("payload")
    if not isinstance(payload, list):
        return None

    rules: list[str] = []
    for item in payload:
        rule = clean_rule(item)
        if rule:
            rules.append(rule)

    return rules


def parse_text_rules(content: str) -> list[str]:
    rules: list[str] = []

    for line in content.splitlines():
        rule = clean_rule(line)
        if rule:
            rules.append(rule)

    return rules


def deduplicate_rules(rules: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []

    for rule in rules:
        if rule in seen:
            continue
        seen.add(rule)
        result.append(rule)

    return result


def parse_rules(content: str) -> tuple[list[str], str]:
    yaml_rules = parse_yaml_payload(content)
    if yaml_rules is not None:
        return deduplicate_rules(yaml_rules), "yaml-payload"

    text_rules = parse_text_rules(content)
    return deduplicate_rules(text_rules), "text"


def write_text_rules(path: Path, rules: list[str]) -> None:
    path.write_text("\n".join(rules) + "\n", encoding="utf-8")


def run_mihomo_convert(behavior: str, input_file: Path, output_file: Path) -> None:
    command = [
        "mihomo",
        "convert-ruleset",
        behavior,
        "text",
        str(input_file),
        str(output_file),
    ]

    log("Running: " + " ".join(command))
    subprocess.run(command, check=True)


def validate_source(source: dict[str, Any]) -> None:
    required_fields = ["name", "url", "behavior", "output"]

    for field in required_fields:
        if field not in source:
            raise ValueError(f"Missing required field: {field}")

    behavior = str(source["behavior"])
    if behavior not in VALID_BEHAVIORS:
        valid = ", ".join(sorted(VALID_BEHAVIORS))
        raise ValueError(f"Invalid behavior '{behavior}', expected one of: {valid}")

    output = str(source["output"])
    if "/" in output or "\\" in output or not output.strip():
        raise ValueError(f"Invalid output name: {output}")


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_FILE}")

    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))

    if not isinstance(config, dict):
        raise ValueError("Config must be a YAML object")

    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Config must contain a non-empty 'sources' list")

    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Every source must be a YAML object")
        validate_source(source)

    return config


def build_source(source: dict[str, Any]) -> dict[str, Any]:
    name = str(source["name"])
    url = str(source["url"])
    behavior = str(source["behavior"])
    output = str(source["output"])

    group(f"Build {name}")

    try:
        log(f"Name: {name}")
        log(f"URL: {url}")
        log(f"Behavior: {behavior}")
        log(f"Output: {output}")

        content = download(url)
        rules, input_format = parse_rules(content)

        if not rules:
            raise RuntimeError(f"No valid rules found for source: {name}")

        list_file = DIST_DIR / f"{output}.list"
        mrs_file = DIST_DIR / f"{output}.mrs"

        write_text_rules(list_file, rules)
        run_mihomo_convert(behavior, list_file, mrs_file)

        if not mrs_file.exists() or mrs_file.stat().st_size == 0:
            raise RuntimeError(f"MRS file was not generated correctly: {mrs_file}")

        result = {
            "name": name,
            "url": url,
            "behavior": behavior,
            "output": output,
            "input_format": input_format,
            "rules": len(rules),
            "list_file": str(list_file.relative_to(ROOT)),
            "mrs_file": str(mrs_file.relative_to(ROOT)),
            "mrs_size": mrs_file.stat().st_size,
        }

        log("")
        log("Build result:")
        log(f"  input format: {result['input_format']}")
        log(f"  rules: {result['rules']}")
        log(f"  list: {result['list_file']}")
        log(f"  mrs: {result['mrs_file']}")
        log(f"  mrs size: {result['mrs_size']} bytes")

        return result
    finally:
        end_group()


def print_summary(results: list[dict[str, Any]]) -> None:
    group("Build Summary")

    total_rules = sum(int(item["rules"]) for item in results)
    total_mrs_size = sum(int(item["mrs_size"]) for item in results)

    log(f"Generated rulesets: {len(results)}")
    log(f"Total rules: {total_rules}")
    log(f"Total MRS size: {total_mrs_size} bytes")
    log("")

    for item in results:
        log(
            "- "
            f"{item['name']}: "
            f"{item['rules']} rules, "
            f"{item['behavior']}, "
            f"{item['input_format']}, "
            f"{item['mrs_file']} "
            f"({item['mrs_size']} bytes)"
        )

    end_group()

    notice(
        f"Generated {len(results)} rulesets, "
        f"{total_rules} rules, "
        f"{total_mrs_size} bytes total"
    )


def main() -> int:
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    config = load_config()
    results: list[dict[str, Any]] = []

    for source in config["sources"]:
        results.append(build_source(source))

    print_summary(results)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        error(str(exc))
        raise SystemExit(1)
