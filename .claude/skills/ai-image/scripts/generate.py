# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""ai-image: generate images with gpt-image-2 through Kie AI.

Standard library only. Run with:
    uv run .claude/skills/ai-image/scripts/generate.py doctor
    uv run .claude/skills/ai-image/scripts/generate.py image --prompt "..." --title "..."
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SUBMIT_URL = "https://api.kie.ai/api/v1/jobs/createTask"
POLL_URL = "https://api.kie.ai/api/v1/jobs/recordInfo?taskId="
UPLOAD_URL = "https://kieai.redpandaai.co/api/file-base64-upload"

# Trap 2: the upload host and the result CDN sit behind Cloudflare and answer
# urllib's default User-Agent with a 403 (error 1010). Send this on EVERY request.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ai-image/1.0"

MODEL_T2I = "gpt-image-2-text-to-image"
MODEL_I2I = "gpt-image-2-image-to-image"

ASPECT_RATIOS = [
    "auto", "1:1", "3:2", "2:3", "4:3", "3:4", "5:4", "4:5", "16:9", "9:16",
    "2:1", "1:2", "3:1", "1:3", "21:9", "9:21",
]
RESOLUTIONS = ["1K", "2K", "4K"]

MAX_PROMPT_CHARS = 20000
MAX_INPUT_IMAGES = 16
MAX_REF_BYTES = 10 * 1024 * 1024

POLL_INTERVAL = 12
POLL_TIMEOUT = 900
API_TIMEOUT = 60
UPLOAD_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 300

SCRIPT_PATH = Path(__file__).resolve()
SKILL_DIR = SCRIPT_PATH.parent.parent            # .claude/skills/ai-image
REPO_ROOT = SCRIPT_PATH.parents[4]               # the folder that contains .claude/
CONFIG_PATH = SKILL_DIR / "config.json"
ENV_PATH = REPO_ROOT / ".env"

# Distinct exit codes (2 is argparse's own usage error).
EXIT_MISSING_KEY = 3
EXIT_MISSING_FILE = 4
EXIT_REF_TOO_LARGE = 5
EXIT_BAD_PARAMS = 6
EXIT_KIE_API = 7
EXIT_MODEL_404 = 8
EXIT_TIMEOUT = 9
EXIT_DOWNLOAD = 10
EXIT_OUTPUT_DIR = 11
EXIT_UPLOAD = 12
EXIT_NETWORK = 13
EXIT_CONFIG = 14


class SkillError(Exception):
    """An expected failure: carries what went wrong, what to do, and an exit code."""

    def __init__(self, code, problem, fix):
        super().__init__(problem)
        self.code = code
        self.problem = problem
        self.fix = fix


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Config and key loading
# --------------------------------------------------------------------------

def load_env_file(path):
    """Minimal .env parser: KEY=VALUE per line, skip blanks and # comments,
    strip surrounding quotes. Never overwrites variables already in os.environ."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def load_config():
    config = {"output_dir": "generations", "copy_to_downloads": True}
    if CONFIG_PATH.is_file():
        try:
            config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except ValueError as exc:
            raise SkillError(
                EXIT_CONFIG,
                f"config.json is not valid JSON ({exc}).",
                f"Fix the syntax in {CONFIG_PATH}.",
            )
    return config


def resolve_output_dir(config):
    out = Path(str(config.get("output_dir", "generations"))).expanduser()
    return out if out.is_absolute() else REPO_ROOT / out


def get_key():
    key = os.environ.get("KIE_API_KEY", "").strip()
    if not key:
        raise SkillError(
            EXIT_MISSING_KEY,
            "KIE_API_KEY is not set.",
            "Get a key at https://kie.ai, then store it as a line "
            f"KIE_API_KEY=<your key> in {ENV_PATH} "
            "(run `doctor --init-env` to create the placeholder line). "
            "Do not paste the key into chat.",
        )
    return key


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def api_headers(key):
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def model_404_error(model):
    # Trap 5: a 404 means the slug drifted. Never guess a replacement id.
    return SkillError(
        EXIT_MODEL_404,
        f"Kie returned 404 for model id '{model}'. The model slug has drifted on "
        "Kie's side; this is not a bug in the script.",
        "Open https://kie.ai/market, copy the current gpt-image-2 model id, and "
        "update MODEL_T2I / MODEL_I2I at the top of generate.py.",
    )


def kie_status_error(status, msg, what, model=None):
    if status == 404 and model:
        return model_404_error(model)
    if status == 401:
        fix = f"Check that KIE_API_KEY in {ENV_PATH} is correct and still active (https://kie.ai)."
    elif status == 402:
        fix = "The Kie account is out of credits. Top up at https://kie.ai, then rerun."
    elif status == 429:
        fix = "Kie is rate limiting this key. Wait a minute, then rerun."
    else:
        fix = "Read the Kie message above, correct the request, and rerun. If it persists, check https://kie.ai for service status."
    return SkillError(EXIT_KIE_API, f"Kie API error during {what} (code {status}): {msg}", fix)


def api_call(method, url, key, what, payload=None, timeout=API_TIMEOUT, model=None):
    """Call a Kie JSON endpoint and return the parsed response body."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=api_headers(key))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        raise kie_status_error(exc.code, body or exc.reason, what, model)
    except (urllib.error.URLError, OSError) as exc:
        raise SkillError(
            EXIT_NETWORK,
            f"Network error during {what}: {getattr(exc, 'reason', exc)}",
            "Check the internet connection (and any proxy or firewall), then rerun.",
        )
    try:
        return json.loads(raw), raw
    except ValueError:
        raise SkillError(
            EXIT_KIE_API,
            f"Kie returned a non-JSON response during {what}: {raw[:500]}",
            "Rerun in a minute; if it persists, check https://kie.ai for service status.",
        )


def check_envelope(body, what, model=None):
    """Kie also reports errors as HTTP 200 with a non-200 `code` in the body."""
    code = body.get("code") if isinstance(body, dict) else None
    if code not in (None, 200):
        raise kie_status_error(code, body.get("msg", ""), what, model)


# --------------------------------------------------------------------------
# Kie operations
# --------------------------------------------------------------------------

def upload_reference(path, key):
    """Upload a local file and return its public URL."""
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    payload_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    body = {
        # Trap 3: the full data: prefix is required; a bare payload is silently rejected.
        "base64Data": f"data:{mime};base64,{payload_b64}",
        "uploadPath": "images/generate",
        "fileName": path.name,
    }
    resp, raw = api_call("POST", UPLOAD_URL, key, f"upload of {path.name}",
                         payload=body, timeout=UPLOAD_TIMEOUT)
    # The envelope has shipped in several shapes; probe them in order.
    data = resp.get("data") if isinstance(resp, dict) else None
    candidates = []
    if isinstance(data, dict):
        candidates += [data.get("fileUrl"), data.get("url"), data.get("downloadUrl")]
    if isinstance(resp, dict):
        candidates += [resp.get("fileUrl"), resp.get("url")]
    for value in candidates:
        if isinstance(value, str) and value.startswith("http"):
            return value
    raise SkillError(
        EXIT_UPLOAD,
        f"Upload of {path.name} returned no file URL. Raw response: {raw}",
        "Check the raw response above for an error message from Kie; if the "
        "URL moved to a new field, add that field to upload_reference().",
    )


def submit_task(model, task_input, key):
    resp, raw = api_call("POST", SUBMIT_URL, key, "task submit",
                         payload={"model": model, "input": task_input}, model=model)
    check_envelope(resp, "task submit", model)
    data = resp.get("data") if isinstance(resp, dict) else None
    task_id = data.get("taskId") if isinstance(data, dict) else None
    if not task_id:
        raise SkillError(
            EXIT_KIE_API,
            f"Kie accepted the submit but returned no data.taskId. Raw response: {raw}",
            "Rerun; if it persists, check https://kie.ai for service status.",
        )
    return task_id


def poll_task(task_id, key):
    """Poll until success and return the result URL."""
    deadline = time.monotonic() + POLL_TIMEOUT
    last_state = None
    while True:
        resp, raw = api_call("GET", POLL_URL + task_id, key, "status poll")
        check_envelope(resp, "status poll")
        data = resp.get("data") if isinstance(resp, dict) else None
        data = data if isinstance(data, dict) else {}
        state = data.get("state")
        if state != last_state:
            log(f"[kie] {state}")
            last_state = state
        if state == "success":
            result = data.get("resultJson")
            # Trap 1: resultJson is a JSON *string* nested inside the JSON response.
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    result = None
            urls = result.get("resultUrls") if isinstance(result, dict) else None
            if not urls or not isinstance(urls[0], str):
                raise SkillError(
                    EXIT_KIE_API,
                    f"Task {task_id} succeeded but resultJson has no resultUrls[0]. Raw response: {raw}",
                    "Rerun the generation; if it persists, check https://kie.ai/market for a changed response format.",
                )
            return urls[0]
        if state == "fail":
            raise SkillError(
                EXIT_KIE_API,
                str(data.get("failMsg")),  # verbatim, no paraphrasing
                "That is Kie's failure message, verbatim. Adjust the prompt or "
                "reference images accordingly and rerun.",
            )
        if time.monotonic() + POLL_INTERVAL > deadline:
            raise SkillError(
                EXIT_TIMEOUT,
                f"Task {task_id} did not finish within {POLL_TIMEOUT} seconds (last state: {state}).",
                "Kie is slow or stuck. Rerun later, or try a lower resolution.",
            )
        time.sleep(POLL_INTERVAL)


def download(url, dest):
    """Download with the browser User-Agent, a 300 s timeout and one silent retry."""
    last_exc = None
    for _attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
                tmp = dest.with_name(dest.name + ".part")
                with open(tmp, "wb") as fh:
                    shutil.copyfileobj(resp, fh)
                os.replace(tmp, dest)
                return
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
    raise SkillError(
        EXIT_DOWNLOAD,
        f"Could not download the finished image (tried twice): {last_exc}",
        "Check the connection and rerun the generation soon; Kie result URLs "
        "expire within hours, so the result cannot be fetched later.",
    )


# --------------------------------------------------------------------------
# Local validation and output layout
# --------------------------------------------------------------------------

def validate_params(prompt, aspect_ratio, resolution, refs):
    if not prompt.strip():
        raise SkillError(EXIT_BAD_PARAMS, "The prompt is empty.", "Pass a non-empty --prompt.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise SkillError(
            EXIT_BAD_PARAMS,
            f"The prompt is {len(prompt)} characters; the limit is {MAX_PROMPT_CHARS}.",
            f"Shorten the prompt to {MAX_PROMPT_CHARS} characters or fewer.",
        )
    if aspect_ratio not in ASPECT_RATIOS:
        raise SkillError(
            EXIT_BAD_PARAMS,
            f"Unsupported aspect ratio '{aspect_ratio}'.",
            "Use one of: " + ", ".join(ASPECT_RATIOS) + ".",
        )
    if resolution is not None:
        if resolution not in RESOLUTIONS:
            raise SkillError(
                EXIT_BAD_PARAMS,
                f"Unsupported resolution '{resolution}'.",
                "Use one of: " + ", ".join(RESOLUTIONS) + " (exact capitalisation).",
            )
        if aspect_ratio == "auto" and resolution != "1K":
            raise SkillError(
                EXIT_BAD_PARAMS,
                f"Aspect ratio 'auto' supports 1K only, but {resolution} was requested.",
                f"Pass an explicit --aspect-ratio (for example 3:2 or 16:9) together with --resolution {resolution}.",
            )
        if aspect_ratio == "1:1" and resolution == "4K":
            raise SkillError(
                EXIT_BAD_PARAMS,
                "Aspect ratio 1:1 cannot reach 4K.",
                "Use --resolution 2K with 1:1, or pick a non-square aspect ratio for 4K.",
            )
    if len(refs) > MAX_INPUT_IMAGES:
        raise SkillError(
            EXIT_BAD_PARAMS,
            f"{len(refs)} reference images were passed; the limit is {MAX_INPUT_IMAGES}.",
            f"Pass at most {MAX_INPUT_IMAGES} --input-image files.",
        )
    for ref in refs:
        if not ref.is_file():
            raise SkillError(
                EXIT_MISSING_FILE,
                f"Reference image not found: {ref}",
                "Check the path passed to --input-image (reference images belong in generations/refs/).",
            )
        size = ref.stat().st_size
        if size > MAX_REF_BYTES:
            raise SkillError(
                EXIT_REF_TOO_LARGE,
                f"Reference image {ref.name} is {size / 1048576:.1f} MB; the limit is 10 MB.",
                "Downscale it and retry. A 2K reference is plenty.",
            )


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "untitled"


def next_image_path(folder):
    highest = 0
    if folder.is_dir():
        for entry in folder.iterdir():
            match = re.fullmatch(r"image-(\d+)\.png", entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return folder / f"image-{highest + 1:02d}.png"


def ensure_dir(folder):
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SkillError(
            EXIT_OUTPUT_DIR,
            f"Cannot create the output folder {folder}: {exc}",
            "Fix the folder permissions, or change output_dir in "
            f"{CONFIG_PATH}, or pass --filename.",
        )


def write_records(image_path, model, prompt, params, ref_names, created):
    folder = image_path.parent
    prompt_md = folder / "prompt.md"
    if not prompt_md.exists():
        prompt_md.write_text(
            f"# {folder.name}\n\nCreated {created}\n", encoding="utf-8")
    refs_text = ", ".join(ref_names) if ref_names else "none"
    params_text = "\n".join(f"- {k}: {v}" for k, v in params.items())
    with open(prompt_md, "a", encoding="utf-8") as fh:
        fh.write(
            f"\n## {image_path.name} ({created})\n\n"
            f"Model: {model}\n\n"
            f"Parameters sent:\n{params_text}\n\n"
            f"Reference images: {refs_text}\n\n"
            f"Prompt:\n\n{prompt}\n"
        )
    sidecar = image_path.with_name(image_path.name + ".json")
    sidecar.write_text(json.dumps({
        "model": model,
        "prompt": prompt,
        "refs": ref_names,
        "params": params,
        "created": created,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def copy_to_downloads(image_path):
    """Copy into ~/Downloads/, suffixing rather than overwriting. Returns the
    copy's path, or None if the copy could not be made."""
    try:
        downloads = Path.home() / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        target = downloads / image_path.name
        counter = 1
        while target.exists():
            target = downloads / f"{image_path.stem}-{counter}{image_path.suffix}"
            counter += 1
        shutil.copy2(image_path, target)
        return target
    except OSError as exc:
        log(f"[warn] Could not copy the image to ~/Downloads ({exc}). "
            f"The image itself is safe at {image_path}.")
        return None


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------

def nearest_existing(path):
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def cmd_doctor(args):
    config = load_config()
    out_dir = resolve_output_dir(config)
    env_note = None

    if args.init_env:
        existing = ENV_PATH.read_text(encoding="utf-8", errors="replace") if ENV_PATH.is_file() else ""
        if re.search(r"^\s*KIE_API_KEY\s*=", existing, flags=re.M):
            env_note = f"{ENV_PATH} already has a KIE_API_KEY line; left unchanged."
        else:
            with open(ENV_PATH, "a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write("KIE_API_KEY=\n")
            env_note = f"Added a placeholder line KIE_API_KEY= to {ENV_PATH}."

    key_present = bool(os.environ.get("KIE_API_KEY", "").strip())
    probe = nearest_existing(out_dir)
    dir_ok = probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)
    ok = key_present and dir_ok

    if args.json:
        print(json.dumps({
            "ok": ok,
            "key_present": key_present,
            "env_file": str(ENV_PATH),
            "output_dir": str(out_dir),
            "output_dir_writable": dir_ok,
            "env_note": env_note,
        }))
    else:
        print(f"KIE_API_KEY : {'present' if key_present else 'MISSING'}")
        print(f"output dir  : {out_dir} ({'writable' if dir_ok else 'NOT WRITABLE'})")
        if env_note:
            print(env_note)
        if not key_present:
            print("Fix: get a key at https://kie.ai and put it after KIE_API_KEY= in "
                  f"{ENV_PATH}. Do not paste the key into chat."
                  + ("" if args.init_env or ENV_PATH.is_file() else
                     " Run `doctor --init-env` to create the placeholder line."))
        if not dir_ok:
            print(f"Fix: make {probe} writable, or change output_dir in {CONFIG_PATH}.")
        if ok:
            print("Ready.")

    if not key_present:
        return EXIT_MISSING_KEY
    if not dir_ok:
        return EXIT_OUTPUT_DIR
    return 0


def cmd_image(args):
    config = load_config()
    key = get_key()

    refs = [Path(p).expanduser() for p in (args.input_image or [])]
    validate_params(args.prompt, args.aspect_ratio, args.resolution, refs)

    # Trap 4: a reference image changes the model id, not just the input fields.
    model = MODEL_I2I if refs else MODEL_T2I

    # Settle the output folder before spending credits.
    if args.filename:
        image_path = Path(args.filename).expanduser().resolve()
        ensure_dir(image_path.parent)
    else:
        folder = resolve_output_dir(config) / (
            time.strftime("%Y-%m-%d") + "-" + slugify(args.title or "untitled"))
        ensure_dir(folder)
        image_path = next_image_path(folder)

    params = {"aspect_ratio": args.aspect_ratio}
    if args.resolution:
        params["resolution"] = args.resolution

    task_input = {"prompt": args.prompt, **params}
    if refs:
        urls = []
        for ref in refs:
            log(f"[kie] uploading {ref.name}")
            urls.append(upload_reference(ref, key))
        task_input["input_urls"] = urls

    log(f"[kie] submitting to {model}")
    task_id = submit_task(model, task_input, key)
    result_url = poll_task(task_id, key)

    log("[kie] downloading result")
    download(result_url, image_path)

    created = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    created = created[:-2] + ":" + created[-2:] if re.search(r"[+-]\d{4}$", created) else created
    write_records(image_path, model, args.prompt, params, [r.name for r in refs], created)

    downloads_copy = None
    if config.get("copy_to_downloads", True):
        downloads_copy = copy_to_downloads(image_path)

    print(json.dumps({
        "image_path": str(image_path),
        "folder": str(image_path.parent),
        "model": model,
        "downloads_copy": str(downloads_copy) if downloads_copy else None,
    }))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="generate.py", description="Generate images with gpt-image-2 through Kie AI.")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check the API key and the output directory")
    doctor.add_argument("--json", action="store_true", help="print the status as JSON")
    doctor.add_argument("--init-env", action="store_true",
                        help="create or append a KIE_API_KEY= placeholder in .env")
    doctor.set_defaults(func=cmd_doctor)

    image = sub.add_parser("image", help="generate one image")
    image.add_argument("--prompt", required=True)
    image.add_argument("--title", help="kebab-case working title, used for the output folder name")
    image.add_argument("--aspect-ratio", default="auto")
    image.add_argument("--resolution", help="1K, 2K or 4K (omit unless asked for)")
    image.add_argument("--input-image", action="append", metavar="PATH",
                       help="local reference image; repeatable")
    image.add_argument("--filename", metavar="PATH",
                       help="override the auto-generated output path entirely")
    image.set_defaults(func=cmd_image)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        load_env_file(ENV_PATH)
        return args.func(args)
    except SkillError as exc:
        log(f"ERROR: {exc.problem}")
        log(f"FIX: {exc.fix}")
        return exc.code
    except KeyboardInterrupt:
        log("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
