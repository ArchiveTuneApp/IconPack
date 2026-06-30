#!/usr/bin/env python3

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ElementTree
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse


METADATA_START = "<!-- ICON_METADATA_START -->"
METADATA_END = "<!-- ICON_METADATA_END -->"
REQUIRED_FIELDS = ("name", "author", "file")
ALLOWED_FIELDS = frozenset((*REQUIRED_FIELDS, "link"))
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
MAX_SVG_BYTES = 1_000_000
SVG_DOCTYPE_PATTERN = re.compile(
    rb"<!DOCTYPE\s+svg(?:\s+(?:PUBLIC|SYSTEM)\s+"
    rb"(?:\"[^\"]*\"|'[^']*')"
    rb"(?:\s+(?:\"[^\"]*\"|'[^']*'))?)?\s*>",
    re.IGNORECASE,
)
PACKAGE_NAME_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)
MAX_FILENAME_COMPONENT_LENGTH = 48


class SubmissionError(Exception):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("pull-request", "rebuild"), required=True)
    parser.add_argument("--event-path", type=Path)
    parser.add_argument("--base-sha")
    parser.add_argument("--valkyrie", type=Path, required=True)
    parser.add_argument("--s2v", type=Path, required=True)
    parser.add_argument("--package-name", required=True)
    parser.add_argument("--random-digits", type=int, choices=(6,), default=6)
    return parser.parse_args()


def run(*command: str) -> str:
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def read_pr_body(event_path: Path) -> str:
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
        body = event["pull_request"]["body"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise SubmissionError(f"Cannot read the pull request body: {error}") from error
    if not isinstance(body, str):
        raise SubmissionError("The pull request body is empty.")
    return body


def read_pr_author_url(event_path: Path) -> str:
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
        login = event["pull_request"]["user"]["login"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise SubmissionError(
            f"Cannot read the pull request author: {error}"
        ) from error
    if not isinstance(login, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login
    ):
        raise SubmissionError("The pull request author login is invalid.")
    return f"https://github.com/{login}"


def parse_form(body: str) -> list[dict[str, str]]:
    start = body.find(METADATA_START)
    end = body.find(METADATA_END)
    markers_missing = start < 0 and end < 0
    if not markers_missing and (start < 0 or end < 0 or end <= start):
        raise SubmissionError("The icon metadata markers are missing or out of order.")

    if markers_missing:
        json_content = body.strip()
    else:
        section = body[start + len(METADATA_START) : end].strip()
        fenced = re.fullmatch(r"```json\s*(.*?)\s*```", section, re.DOTALL)
        if fenced is None:
            raise SubmissionError(
                "Icon metadata must remain inside the template's JSON block."
            )
        json_content = fenced.group(1)

    try:
        form = json.loads(json_content)
    except json.JSONDecodeError as error:
        raise SubmissionError(f"Icon metadata is not valid JSON: {error}") from error

    if not isinstance(form, dict) or set(form) != {"icons"}:
        raise SubmissionError('Icon metadata must contain only an "icons" array.')
    raw_icons = form["icons"]
    if not isinstance(raw_icons, list) or not raw_icons:
        raise SubmissionError('The "icons" array must contain at least one icon.')

    icons: list[dict[str, str]] = []
    seen_files: set[str] = set()
    for index, raw_icon in enumerate(raw_icons, start=1):
        if not isinstance(raw_icon, dict):
            raise SubmissionError(f"Icon #{index} must be a JSON object.")
        unknown_fields = set(raw_icon) - ALLOWED_FIELDS
        if unknown_fields:
            fields = ", ".join(sorted(unknown_fields))
            raise SubmissionError(f"Icon #{index} contains unsupported fields: {fields}.")

        icon: dict[str, str] = {}
        for field in REQUIRED_FIELDS:
            value = raw_icon.get(field)
            if not isinstance(value, str) or not value.strip():
                raise SubmissionError(f'Icon #{index} requires a non-empty "{field}".')
            icon[field] = value.strip()
        if len(icon["name"]) > 120 or len(icon["author"]) > 120:
            raise SubmissionError(
                f"Icon #{index} name and author must not exceed 120 characters."
            )

        link = raw_icon.get("link", "")
        if not isinstance(link, str):
            raise SubmissionError(f'Icon #{index} field "link" must be a string.')
        icon["link"] = link.strip()
        if icon["link"]:
            parsed_link = urlparse(icon["link"])
            if parsed_link.scheme not in {"http", "https"} or not parsed_link.netloc:
                raise SubmissionError(
                    f'Icon #{index} field "link" must be an HTTP(S) URL or empty.'
                )

        file_path = PurePosixPath(icon["file"])
        if (
            file_path.is_absolute()
            or len(file_path.parts) != 2
            or file_path.parts[0] != "submissions"
            or file_path.suffix.lower() != ".svg"
            or file_path.name in {".svg", ".."}
        ):
            raise SubmissionError(
                f'Icon #{index} file must be "submissions/<filename>.svg".'
            )
        normalized_file = file_path.as_posix()
        if normalized_file in seen_files:
            raise SubmissionError(f'Duplicate file in metadata: "{normalized_file}".')
        seen_files.add(normalized_file)
        icon["file"] = normalized_file
        icons.append(icon)

    return icons


def changed_paths(base_sha: str) -> list[tuple[str, str]]:
    output = run(
        "git",
        "diff",
        "--name-status",
        "--no-renames",
        f"{base_sha}...HEAD",
    )
    changes: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        status, path = line.split("\t", maxsplit=1)
        changes.append((status, path))
    return changes


def validate_pr_changes(
    changes: list[tuple[str, str]], icons: list[dict[str, str]]
) -> bool:
    submitted_files = {icon["file"] for icon in icons}
    actual_submissions = {
        path for _, path in changes if path.startswith("submissions/")
    }

    if not actual_submissions:
        generated_change = any(
            path == "metadata.json"
            or path.startswith("pack/")
            or path.startswith("xml/")
            for _, path in changes
        )
        if generated_change:
            print("No unprocessed SVG submissions remain; nothing to do.")
            return False
        raise SubmissionError("No SVG files were added under submissions/.")

    invalid_changes = [
        f"{status} {path}"
        for status, path in changes
        if status != "A" or path not in submitted_files
    ]
    if invalid_changes:
        formatted = "\n  ".join(invalid_changes)
        raise SubmissionError(
            "An icon submission PR may initially add only the SVG files listed "
            f"in its metadata:\n  {formatted}"
        )
    if actual_submissions != submitted_files:
        missing = submitted_files - actual_submissions
        unlisted = actual_submissions - submitted_files
        details = []
        if missing:
            details.append(f"missing: {', '.join(sorted(missing))}")
        if unlisted:
            details.append(f"not listed: {', '.join(sorted(unlisted))}")
        raise SubmissionError("SVG file mismatch (" + "; ".join(details) + ").")
    return True


def validate_svg(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise SubmissionError(f"{path.as_posix()} must be a regular file.")
    size = path.stat().st_size
    if size == 0 or size > MAX_SVG_BYTES:
        raise SubmissionError(
            f"{path.as_posix()} must be non-empty and no larger than "
            f"{MAX_SVG_BYTES // 1_000_000} MB."
        )
    svg_bytes = path.read_bytes()
    if svg_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SubmissionError(
            f"{path.as_posix()} contains PNG data. Export and submit an actual "
            "SVG XML file instead of renaming a PNG file."
        )
    upper_svg_bytes = svg_bytes.upper()
    if b"<!ENTITY" in upper_svg_bytes:
        raise SubmissionError(f"{path.as_posix()} must not declare an entity.")
    sanitized_svg_bytes, doctype_count = SVG_DOCTYPE_PATTERN.subn(
        b"", svg_bytes, count=1
    )
    if b"<!DOCTYPE" in sanitized_svg_bytes.upper():
        raise SubmissionError(
            f"{path.as_posix()} contains an unsupported DTD declaration."
        )
    try:
        root = ElementTree.fromstring(sanitized_svg_bytes)
    except ElementTree.ParseError as error:
        raise SubmissionError(f"{path.as_posix()} is not valid XML: {error}") from error
    if root.tag != f"{{{SVG_NAMESPACE}}}svg" and root.tag != "svg":
        raise SubmissionError(f"{path.as_posix()} does not have an SVG root element.")
    for element in root.iter():
        local_tag = element.tag.rsplit("}", maxsplit=1)[-1].lower()
        if local_tag in {"script", "foreignobject"}:
            raise SubmissionError(
                f"{path.as_posix()} contains unsupported active SVG content."
            )
        for attribute, value in element.attrib.items():
            if (
                attribute.rsplit("}", maxsplit=1)[-1].lower() == "href"
                and value
                and not value.startswith("#")
            ):
                raise SubmissionError(
                    f"{path.as_posix()} contains an external SVG reference."
                )
    if doctype_count:
        path.write_bytes(sanitized_svg_bytes)


def read_metadata(path: Path) -> list[dict[str, Any]]:
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SubmissionError(f"Cannot read metadata.json: {error}") from error
    if not isinstance(metadata, list) or not all(
        isinstance(entry, dict) for entry in metadata
    ):
        raise SubmissionError("metadata.json must be an array of objects.")
    return metadata


def read_metadata_at_revision(revision: str) -> list[dict[str, Any]]:
    try:
        metadata = json.loads(run("git", "show", f"{revision}:metadata.json"))
    except (json.JSONDecodeError, subprocess.CalledProcessError) as error:
        raise SubmissionError(
            f"Cannot read metadata.json at revision {revision}: {error}"
        ) from error
    if not isinstance(metadata, list) or not all(
        isinstance(entry, dict) for entry in metadata
    ):
        raise SubmissionError(
            f"metadata.json at revision {revision} must be an array of objects."
        )
    return metadata


def workflow_message(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def warn_duplicate_authors(
    icons: list[dict[str, str]], metadata: list[dict[str, Any]]
) -> None:
    existing_authors = {
        entry["Author"]
        for entry in metadata
        if isinstance(entry.get("Author"), str)
    }
    duplicate_authors = sorted(
        {
            icon["author"]
            for icon in icons
            if icon["author"] in existing_authors
        }
    )
    for author in duplicate_authors:
        message = workflow_message(
            f'Author "{author}" already exists in metadata.json. '
            "Confirm that this submission uses the intended author name."
        )
        print(f"::warning title=Duplicate author name::{message}")


def used_ids(
    metadata: list[dict[str, Any]], random_digits: int
) -> set[str]:
    identifier_pattern = re.compile(rf"\d{{{random_digits}}}")
    filename_pattern = re.compile(rf"_(\d{{{random_digits}}})(?:\.|$)")
    identifiers = {
        str(entry["Id"])
        for entry in metadata
        if isinstance(entry.get("Id"), (str, int))
        and identifier_pattern.fullmatch(str(entry["Id"]))
    }
    for directory in (Path("pack"), Path("xml")):
        for path in directory.iterdir() if directory.is_dir() else ():
            match = filename_pattern.search(path.name)
            if match:
                identifiers.add(match.group(1))
    return identifiers


def next_id(existing: set[str], random_digits: int) -> str:
    lower_bound = 10 ** (random_digits - 1)
    available_identifiers = 9 * lower_bound
    if len(existing) >= available_identifiers:
        raise SubmissionError(
            f"All {random_digits}-digit icon IDs are already in use."
        )
    while True:
        identifier = str(secrets.randbelow(available_identifiers) + lower_bound)
        if identifier not in existing:
            existing.add(identifier)
            return identifier


def kotlin_filename_component(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    words = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    component = "".join(word[:1].upper() + word[1:] for word in words)
    if not component:
        component = fallback
    return component[:MAX_FILENAME_COMPONENT_LENGTH] or fallback


def xml_filename_component(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    component = re.sub(r"[^a-z0-9]+", "_", ascii_value.lower()).strip("_")
    return component[:MAX_FILENAME_COMPONENT_LENGTH].rstrip("_") or fallback


def kotlin_asset_stem(name: str, author: str, identifier: str) -> str:
    icon_name = kotlin_filename_component(name, "Icon")
    author_name = kotlin_filename_component(author, "Author")
    return f"Icon{icon_name}By{author_name}{identifier}"


def xml_asset_stem(name: str, author: str, identifier: str) -> str:
    icon_name = xml_filename_component(name, "icon")
    author_name = xml_filename_component(author, "author")
    return f"{icon_name}_{author_name}_{identifier}"


def metadata_by_id(
    metadata: list[dict[str, Any]], source: str
) -> dict[str, dict[str, Any]]:
    entries_by_id: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(metadata, start=1):
        identifier = entry.get("Id")
        if not isinstance(identifier, (str, int)):
            raise SubmissionError(f"{source} entry #{index} requires an Id.")
        normalized_identifier = str(identifier)
        if re.fullmatch(r"\d{4}|\d{6}", normalized_identifier) is None:
            raise SubmissionError(
                f'{source} entry #{index} has invalid Id "{normalized_identifier}".'
            )
        if normalized_identifier in entries_by_id:
            raise SubmissionError(
                f'{source} contains duplicate Id "{normalized_identifier}".'
            )
        entries_by_id[normalized_identifier] = entry
    return entries_by_id


def metadata_text(
    entry: dict[str, Any], field: str, identifier: str
) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SubmissionError(
            f'Metadata entry with Id "{identifier}" requires a {field}.'
        )
    return value.strip()


def asset_filename(
    entry: dict[str, Any], field: str, identifier: str
) -> str:
    value = entry.get(field)
    if (
        not isinstance(value, str)
        or not value
        or Path(value).name != value
    ):
        raise SubmissionError(
            f'Metadata entry with Id "{identifier}" requires a valid {field}.'
        )
    return value


def renamed_kotlin_content(
    source: Path, previous_symbol: str, expected_symbol: str
) -> str:
    try:
        content = source.read_text(encoding="utf-8")
    except OSError as error:
        raise SubmissionError(
            f"Cannot read generated Kotlin asset {source.as_posix()}: {error}"
        ) from error
    if previous_symbol == expected_symbol:
        return content

    symbol_pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(previous_symbol)}"
        rf"(?![A-Za-z0-9_])"
    )
    updated_content, replacement_count = symbol_pattern.subn(
        expected_symbol, content
    )
    if replacement_count == 0:
        raise SubmissionError(
            f"{source.as_posix()} does not contain its expected generated "
            f'symbol "{previous_symbol}".'
        )
    return updated_content


def synchronize_metadata_assets(
    metadata: list[dict[str, Any]],
    base_metadata: list[dict[str, Any]],
) -> int:
    entries_by_id = metadata_by_id(metadata, "metadata.json")
    base_entries_by_id = metadata_by_id(
        base_metadata, "base metadata.json"
    )
    updated_entries = [
        (identifier, entry, base_entries_by_id[identifier])
        for identifier, entry in entries_by_id.items()
        if identifier in base_entries_by_id
        and entry != base_entries_by_id[identifier]
    ]
    synchronized_count = 0

    for identifier, entry, base_entry in updated_entries:
        name = metadata_text(entry, "Name", identifier)
        author = metadata_text(entry, "Author", identifier)
        expected_symbol = kotlin_asset_stem(name, author, identifier)
        expected_filename = f"{expected_symbol}.kt"
        expected_source = (
            f"{xml_asset_stem(name, author, identifier)}.xml"
        )
        previous_filename = asset_filename(
            base_entry, "Filename", identifier
        )
        previous_source = asset_filename(base_entry, "Source", identifier)
        previous_symbol = Path(previous_filename).stem
        previous_pack_path = Path("pack") / previous_filename
        expected_pack_path = Path("pack") / expected_filename
        previous_xml_path = Path("xml") / previous_source
        expected_xml_path = Path("xml") / expected_source

        if expected_pack_path != previous_pack_path:
            if expected_pack_path.exists() and previous_pack_path.exists():
                raise SubmissionError(
                    f"Cannot rename {previous_pack_path.as_posix()} because "
                    f"{expected_pack_path.as_posix()} already exists."
                )
            if previous_pack_path.is_file():
                updated_content = renamed_kotlin_content(
                    previous_pack_path,
                    previous_symbol,
                    expected_symbol,
                )
                expected_pack_path.write_text(
                    updated_content, encoding="utf-8"
                )
                previous_pack_path.unlink()
            elif not expected_pack_path.is_file():
                raise SubmissionError(
                    f'Cannot find Kotlin asset for Id "{identifier}".'
                )
        elif expected_pack_path.is_file():
            updated_content = renamed_kotlin_content(
                expected_pack_path,
                previous_symbol,
                expected_symbol,
            )
            if updated_content != expected_pack_path.read_text(encoding="utf-8"):
                expected_pack_path.write_text(
                    updated_content, encoding="utf-8"
                )
        else:
            raise SubmissionError(
                f'Cannot find Kotlin asset for Id "{identifier}".'
            )

        if expected_xml_path != previous_xml_path:
            if expected_xml_path.exists() and previous_xml_path.exists():
                raise SubmissionError(
                    f"Cannot rename {previous_xml_path.as_posix()} because "
                    f"{expected_xml_path.as_posix()} already exists."
                )
            if previous_xml_path.is_file():
                previous_xml_path.rename(expected_xml_path)
            elif not expected_xml_path.is_file():
                raise SubmissionError(
                    f'Cannot find XML asset for Id "{identifier}".'
                )
        elif not expected_xml_path.is_file():
            raise SubmissionError(
                f'Cannot find XML asset for Id "{identifier}".'
            )

        entry["Id"] = identifier
        entry["Name"] = name
        entry["Author"] = author
        entry["Filename"] = expected_filename
        entry["Source"] = expected_source
        synchronized_count += 1

    return synchronized_count


def convert_icon(
    valkyrie: Path,
    source: Path,
    destination: Path,
    generated_name: str,
    package_name: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="valkyrie-") as temporary_directory:
        temporary = Path(temporary_directory)
        staged_svg = temporary / f"{generated_name}.svg"
        output = temporary / "output"
        shutil.copyfile(source, staged_svg)
        try:
            subprocess.run(
                [
                    str(valkyrie),
                    "svgxml2imagevector",
                    "--input-path",
                    str(staged_svg),
                    "--output-path",
                    str(output),
                    "--package-name",
                    package_name,
                    "--flatpackage",
                    "true",
                    "--output-format",
                    "lazy-property",
                    "--generate-preview",
                    "false",
                    "--trailing-comma",
                    "true",
                ],
                check=True,
            )
        except subprocess.CalledProcessError as error:
            raise SubmissionError(
                f"Valkyrie could not convert {source.as_posix()}."
            ) from error
        generated = output / f"{generated_name}.kt"
        if not generated.is_file():
            raise SubmissionError(
                f"Valkyrie did not produce the expected {generated_name}.kt file."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(generated, destination)


def convert_android_vector(s2v: Path, source: Path, destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="vector-drawable-") as temporary_directory:
        generated = Path(temporary_directory) / destination.name
        try:
            subprocess.run(
                [
                    str(s2v),
                    "--input",
                    str(source),
                    "--output",
                    str(generated),
                ],
                check=True,
            )
        except subprocess.CalledProcessError as error:
            raise SubmissionError(
                f"svg2vectordrawable could not convert {source.as_posix()}."
            ) from error
        if not generated.is_file():
            raise SubmissionError(
                f"svg2vectordrawable did not produce the expected "
                f"{destination.name} file."
            )
        try:
            root = ElementTree.parse(generated).getroot()
        except (ElementTree.ParseError, OSError) as error:
            raise SubmissionError(
                f"svg2vectordrawable produced invalid XML for {source.as_posix()}."
            ) from error
        if root.tag != "vector":
            raise SubmissionError(
                f"svg2vectordrawable did not produce an Android vector drawable "
                f"for {source.as_posix()}."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(generated, destination)


def process_pull_request(args: argparse.Namespace) -> None:
    if args.event_path is None or not args.base_sha:
        raise SubmissionError(
            "Pull-request mode requires --event-path and --base-sha."
        )
    changes = changed_paths(args.base_sha)
    if not any(path.startswith("submissions/") for _, path in changes):
        if not any(path == "metadata.json" for _, path in changes):
            print("No unprocessed SVG submissions remain; nothing to do.")
            return

        metadata_path = Path("metadata.json")
        metadata = read_metadata(metadata_path)
        base_metadata = read_metadata_at_revision(args.base_sha)
        synchronized_count = synchronize_metadata_assets(
            metadata, base_metadata
        )
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"Synchronized {synchronized_count} metadata asset set(s)."
        )
        return

    icons = parse_form(read_pr_body(args.event_path))
    github_author_url = read_pr_author_url(args.event_path)
    if not validate_pr_changes(changes, icons):
        return

    metadata_path = Path("metadata.json")
    metadata = read_metadata(metadata_path)
    base_metadata = read_metadata_at_revision(args.base_sha)
    warn_duplicate_authors(icons, base_metadata)
    identifiers = used_ids(metadata, args.random_digits)
    Path("pack").mkdir(exist_ok=True)
    Path("xml").mkdir(exist_ok=True)

    for icon in icons:
        submission = Path(icon["file"])
        validate_svg(submission)
        identifier = next_id(identifiers, args.random_digits)
        generated_name = kotlin_asset_stem(
            icon["name"], icon["author"], identifier
        )
        kotlin_filename = f"{generated_name}.kt"
        xml_filename = (
            f"{xml_asset_stem(icon['name'], icon['author'], identifier)}.xml"
        )

        convert_icon(
            valkyrie=args.valkyrie,
            source=submission,
            destination=Path("pack") / kotlin_filename,
            generated_name=generated_name,
            package_name=args.package_name,
        )
        convert_android_vector(
            s2v=args.s2v,
            source=submission,
            destination=Path("xml") / xml_filename,
        )
        submission.unlink()
        metadata.append(
            {
                "Id": identifier,
                "Name": icon["name"],
                "Author": icon["author"],
                "Filename": kotlin_filename,
                "Source": xml_filename,
                "Submission": icon["file"],
                "Link": icon["link"],
                "GitHubAuthorUrl": github_author_url,
            }
        )

    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def metadata_matches_submission(
    entry: dict[str, Any], submission: Path
) -> bool:
    identifier = str(entry.get("Id", ""))
    source = entry.get("Source")
    original_submission = entry.get("Submission")
    filename = entry.get("Filename")
    return (
        original_submission == submission.as_posix()
        or source == submission.name
        or (
            isinstance(filename, str)
            and Path(filename).name == filename
            and Path(filename).stem == submission.stem
        )
        or (re.fullmatch(r"\d{4}|\d{6}", identifier) is not None
            and submission.name == f"{identifier}.svg")
    )


def reconcile_icon_pack(
    metadata: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    retained_metadata: list[dict[str, Any]] = []
    referenced_pack_files: set[str] = set()
    referenced_xml_files: set[str] = set()
    removed_metadata_count = 0

    for entry in metadata:
        filename = entry.get("Filename")
        if (
            not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
        ):
            removed_metadata_count += 1
            continue

        pack_file = Path("pack") / filename
        if pack_file.is_symlink() or not pack_file.is_file():
            removed_metadata_count += 1
            continue

        retained_metadata.append(entry)
        referenced_pack_files.add(filename)
        source = entry.get("Source")
        if (
            isinstance(source, str)
            and source
            and Path(source).name == source
        ):
            referenced_xml_files.add(source)

    removed_file_count = 0
    for directory, referenced_files in (
        (Path("pack"), referenced_pack_files),
        (Path("xml"), referenced_xml_files),
    ):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.name == ".gitkeep":
                continue
            if path.is_dir() and not path.is_symlink():
                continue
            if path.name not in referenced_files:
                path.unlink()
                removed_file_count += 1

    submissions_directory = Path("submissions")
    if submissions_directory.is_dir():
        for path in submissions_directory.iterdir():
            if path.name == ".gitkeep":
                continue
            if path.is_dir() and not path.is_symlink():
                continue
            path.unlink()
            removed_file_count += 1

    return retained_metadata, removed_metadata_count, removed_file_count


def rebuild_existing_icons(args: argparse.Namespace) -> None:
    metadata_path = Path("metadata.json")
    metadata = read_metadata(metadata_path)
    submissions = sorted(Path("submissions").glob("*.svg"))
    identifiers = used_ids(metadata, args.random_digits)
    rebuilt_count = 0

    for submission in submissions:
        matches = [
            entry
            for entry in metadata
            if metadata_matches_submission(entry, submission)
        ]
        if not matches:
            print(
                f"Skipping {submission.as_posix()}: no matching metadata entry."
            )
            continue
        if len(matches) > 1:
            raise SubmissionError(
                f"{submission.as_posix()} matches multiple metadata entries."
            )

        entry = matches[0]
        identifier = str(entry.get("Id", ""))
        if re.fullmatch(rf"\d{{{args.random_digits}}}", identifier) is None:
            identifier = next_id(identifiers, args.random_digits)
        name = entry.get("Name")
        author = entry.get("Author")
        if not isinstance(name, str) or not name.strip():
            raise SubmissionError(
                f"{submission.as_posix()} metadata requires a Name."
            )
        if not isinstance(author, str) or not author.strip():
            raise SubmissionError(
                f"{submission.as_posix()} metadata requires an Author."
            )
        generated_name = kotlin_asset_stem(name, author, identifier)
        expected_filename = f"{generated_name}.kt"
        previous_filename = entry.get("Filename")
        if not isinstance(previous_filename, str) or not previous_filename:
            raise SubmissionError(
                f"{submission.as_posix()} metadata requires a Filename."
            )
        if Path(previous_filename).name != previous_filename:
            raise SubmissionError(
                f"{submission.as_posix()} metadata Filename must not contain a path."
            )

        validate_svg(submission)
        convert_icon(
            valkyrie=args.valkyrie,
            source=submission,
            destination=Path("pack") / expected_filename,
            generated_name=generated_name,
            package_name=args.package_name,
        )
        previous_asset = Path("pack") / previous_filename
        if previous_asset.name != expected_filename:
            previous_asset.unlink(missing_ok=True)
        previous_source = entry.get("Source")
        generated_xml = Path("xml") / (
            f"{xml_asset_stem(name, author, identifier)}.xml"
        )
        convert_android_vector(
            s2v=args.s2v,
            source=submission,
            destination=generated_xml,
        )
        if (
            isinstance(previous_source, str)
            and Path(previous_source).name == previous_source
            and previous_source != generated_xml.name
        ):
            (Path("xml") / previous_source).unlink(missing_ok=True)
        submission.unlink()
        entry["Id"] = identifier
        entry["Filename"] = expected_filename
        entry["Source"] = generated_xml.name
        entry["Submission"] = submission.as_posix()
        rebuilt_count += 1

    metadata, removed_metadata_count, removed_file_count = reconcile_icon_pack(
        metadata
    )

    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Rebuilt {rebuilt_count} icon(s), removed "
        f"{removed_metadata_count} unused metadata record(s), and removed "
        f"{removed_file_count} unreferenced file(s)."
    )


def process(args: argparse.Namespace) -> None:
    if not PACKAGE_NAME_PATTERN.fullmatch(args.package_name):
        raise SubmissionError("The configured Kotlin package name is invalid.")
    if not args.valkyrie.is_file():
        raise SubmissionError(f"Valkyrie executable not found at {args.valkyrie}.")
    if not args.s2v.is_file():
        raise SubmissionError(
            f"svg2vectordrawable executable not found at {args.s2v}."
        )

    if args.mode == "pull-request":
        process_pull_request(args)
    else:
        rebuild_existing_icons(args)


def main() -> int:
    try:
        process(parse_args())
    except (SubmissionError, subprocess.CalledProcessError, OSError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
