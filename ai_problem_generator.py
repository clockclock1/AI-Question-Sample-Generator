import base64
import hashlib
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, request

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None


LANGUAGES = [
    "C",
    "C With O2",
    "C++",
    "C++ With O2",
    "C++ 17",
    "C++ 17 With O2",
    "C++ 20",
    "C++ 20 With O2",
    "Java",
    "Python3",
    "Python2",
]


SYSTEM_PROMPT = (
    "你是一个算法竞赛题代码生成助手。"
    "请根据题目描述（可能包含截图信息）生成可运行的 Python3 代码，并给出可用于校验的测试样例。"
    "你必须只返回 JSON，不要返回 markdown。JSON 结构必须为："
    "{"
    '"title":"题目标题",'
    '"description":"题目描述",'
    '"input_spec":"输入说明",'
    '"output_spec":"输出说明",'
    '"language":"Python3",'
    '"code":"完整 Python3 代码",'
    '"test_cases":[{"input":"样例输入","output":"样例输出"}]'
    "}"
)


def normalize_base_url(url: str) -> str:
    clean = url.strip().rstrip("/")
    if not clean:
        return ""

    if clean.endswith("/chat/completions"):
        return clean[: -len("/chat/completions")]
    if clean.endswith("/models"):
        return clean[: -len("/models")]
    if clean.endswith("/v1"):
        return clean
    if "/v1/" in clean:
        return clean.split("/v1/")[0] + "/v1"
    return f"{clean}/v1"


def normalize_chat_url(url: str) -> str:
    base = normalize_base_url(url)
    return f"{base}/chat/completions" if base else ""


def normalize_models_url(url: str) -> str:
    base = normalize_base_url(url)
    return f"{base}/models" if base else ""


def file_to_data_url(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        mime = "image/png"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def extract_json_block(text: str) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("AI 返回为空")

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text, re.IGNORECASE)
    if fenced:
        return json.loads(fenced.group(1))

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        return json.loads(candidate)

    raise ValueError("无法从 AI 返回中解析 JSON")


def normalize_text_output(s: str) -> str:
    lines = s.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line.rstrip() for line in lines]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def md5_text(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


@dataclass
class RunResult:
    ok: bool
    actual_output: str
    stderr: str
    return_code: int
    reason: str


def run_python_code_once(code: str, case_input: str, timeout_sec: int = 8) -> RunResult:
    with tempfile.TemporaryDirectory(prefix="ai_problem_runner_") as tmp_dir:
        code_path = os.path.join(tmp_dir, "solution.py")
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(code)

        try:
            proc = subprocess.run(
                [sys.executable, code_path],
                input=case_input,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                ok=False,
                actual_output="",
                stderr="Execution timed out",
                return_code=-1,
                reason="timeout",
            )

        if proc.returncode != 0:
            return RunResult(
                ok=False,
                actual_output=proc.stdout,
                stderr=proc.stderr,
                return_code=proc.returncode,
                reason="runtime_error",
            )

        return RunResult(
            ok=True,
            actual_output=proc.stdout,
            stderr=proc.stderr,
            return_code=proc.returncode,
            reason="ok",
        )


def run_and_validate(
    code: str,
    test_cases: List[Dict[str, str]],
    logger,
) -> Tuple[bool, List[Dict[str, Any]], List[Dict[str, str]]]:
    details: List[Dict[str, Any]] = []
    repaired_cases: List[Dict[str, str]] = []
    all_pass = True

    for idx, case in enumerate(test_cases, start=1):
        inp = case.get("input", "")
        expected = case.get("output", "")
        logger(f"执行第 {idx} 组测试...")
        result = run_python_code_once(code, inp)

        actual_norm = normalize_text_output(result.actual_output)
        expected_norm = normalize_text_output(expected)

        item = {
            "index": idx,
            "input": inp,
            "expected": expected,
            "actual": result.actual_output,
            "stderr": result.stderr,
            "return_code": result.return_code,
            "reason": result.reason,
            "passed": False,
            "compared": False,
        }

        if not result.ok:
            all_pass = False
            item["passed"] = False
            details.append(item)
            repaired_cases.append({"input": inp, "output": expected})
            logger(f"第 {idx} 组测试运行失败：{result.reason}")
            continue

        if expected_norm:
            item["compared"] = True
            if actual_norm == expected_norm:
                item["passed"] = True
                logger(f"第 {idx} 组测试通过")
            else:
                all_pass = False
                item["passed"] = False
                logger(f"第 {idx} 组测试输出不匹配")
            repaired_cases.append({"input": inp, "output": expected})
        else:
            item["compared"] = False
            item["passed"] = True
            repaired_cases.append({"input": inp, "output": actual_norm})
            logger(f"第 {idx} 组测试未提供期望输出，已写入程序实测输出")

        details.append(item)

    return all_pass, details, repaired_cases


def build_failure_report(details: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for d in details:
        if d.get("passed"):
            continue
        lines.append(f"Case {d['index']} failed")
        lines.append("Input:")
        lines.append(d.get("input", ""))
        if d.get("expected"):
            lines.append("Expected:")
            lines.append(d.get("expected", ""))
        lines.append("Actual:")
        lines.append(d.get("actual", ""))
        if d.get("stderr"):
            lines.append("Stderr:")
            lines.append(d.get("stderr", ""))
        lines.append("")
    return "\n".join(lines).strip()


def post_chat_completions_stream(
    api_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, Any]],
    timeout: int = 180,
    on_chunk=None,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "stream": True,
    }

    data = json.dumps(payload).encode("utf-8")
    req = request.Request(api_url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    chunks: List[str] = []

    try:
        with request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue

                data_line = line[len("data:") :].strip()
                if not data_line:
                    continue
                if data_line == "[DONE]":
                    break

                try:
                    event = json.loads(data_line)
                except json.JSONDecodeError:
                    continue

                if "error" in event:
                    raise RuntimeError(f"AI 错误: {json.dumps(event['error'], ensure_ascii=False)}")

                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")

                piece = ""
                if isinstance(content, str):
                    piece = content
                elif isinstance(content, list):
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(str(part.get("text", "")))
                        elif isinstance(part, str):
                            text_parts.append(part)
                    piece = "".join(text_parts)
                elif content:
                    piece = str(content)

                if piece:
                    chunks.append(piece)
                    if on_chunk:
                        on_chunk(piece)
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    except error.URLError as e:
        raise RuntimeError(f"网络错误: {e}") from e

    merged = "".join(chunks)
    if not merged.strip():
        raise RuntimeError("AI 流式响应为空")
    return merged


def fetch_available_models(api_url: str, api_key: str, timeout: int = 30) -> List[str]:
    models_url = normalize_models_url(api_url)
    if not models_url:
        raise ValueError("请先填写 API URL")

    req = request.Request(models_url, method="GET")
    req.add_header("Accept", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"拉取模型失败 HTTP {e.code}: {body}") from e
    except error.URLError as e:
        raise RuntimeError(f"拉取模型失败: {e}") from e

    try:
        payload_json = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"模型接口返回非 JSON: {raw[:500]}") from e

    data_list = payload_json.get("data")
    if not isinstance(data_list, list):
        raise RuntimeError(f"模型接口返回异常: {raw[:500]}")

    model_ids: List[str] = []
    for item in data_list:
        if isinstance(item, dict):
            mid = str(item.get("id", "")).strip()
            if mid:
                model_ids.append(mid)

    if not model_ids:
        raise RuntimeError("未拉取到任何模型")

    return sorted(set(model_ids))


def generate_code_with_ai(
    api_url: str,
    api_key: str,
    model: str,
    problem_text: str,
    image_paths: List[str],
    logger,
    stream_callback=None,
) -> Dict[str, Any]:
    content_parts: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "请根据以下题目内容生成正确代码与样例。"
                "必须返回 JSON 且仅返回 JSON。\n"
                f"题目文本:\n{problem_text}"
            ),
        }
    ]

    for path in image_paths:
        content_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": file_to_data_url(path)},
            }
        )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content_parts},
    ]

    logger("调用 AI 生成代码中（流式）...")
    raw = post_chat_completions_stream(api_url, api_key, model, messages, on_chunk=stream_callback)
    parsed = extract_json_block(raw)
    return parsed


def repair_code_with_ai(
    api_url: str,
    api_key: str,
    model: str,
    problem_text: str,
    previous_code: str,
    failure_report: str,
    test_cases: List[Dict[str, str]],
    logger,
    stream_callback=None,
) -> Dict[str, Any]:
    fix_prompt = (
        "你之前生成的代码未通过测试，请修复。"
        "必须返回 JSON 且仅返回 JSON，结构与之前相同。\n"
        f"题目文本:\n{problem_text}\n\n"
        f"旧代码:\n{previous_code}\n\n"
        f"失败报告:\n{failure_report}\n\n"
        f"测试用例(JSON):\n{json.dumps(test_cases, ensure_ascii=False)}"
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": fix_prompt},
    ]

    logger("调用 AI 修复代码中（流式）...")
    raw = post_chat_completions_stream(api_url, api_key, model, messages, on_chunk=stream_callback)
    parsed = extract_json_block(raw)
    return parsed


def safe_text(value: Optional[str], default: str) -> str:
    v = (value or "").strip()
    return v if v else default


def create_problem_export(
    output_dir: str,
    title: str,
    description: str,
    input_spec: str,
    output_spec: str,
    test_cases: List[Dict[str, str]],
    logger,
) -> Tuple[str, str]:
    ts = int(time.time() * 1000)
    export_name = f"problem_export_{ts}"
    export_path = os.path.join(output_dir, export_name)
    os.makedirs(export_path, exist_ok=True)

    problem_num = ts % 100000
    problem_folder_name = f"problem_{problem_num}"
    problem_folder = os.path.join(export_path, problem_folder_name)
    os.makedirs(problem_folder, exist_ok=True)

    samples = []
    info_cases = []

    for idx, case in enumerate(test_cases, start=1):
        in_name = f"{idx}.in"
        out_name = f"{idx}.out"
        in_path = os.path.join(problem_folder, in_name)
        out_path = os.path.join(problem_folder, out_name)

        case_input = case.get("input", "")
        case_output = case.get("output", "")

        with open(in_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(case_input)
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(case_output)

        output_md5 = md5_text(case_output)
        info_cases.append(
            {
                "caseId": 30000 + idx,
                "inputName": in_name,
                "outputName": out_name,
                "outputMd5": output_md5,
                "outputSize": len(case_output.encode("utf-8")),
                "allStrippedOutputMd5": output_md5,
                "EOFStrippedOutputMd5": output_md5,
            }
        )
        samples.append({"output": out_name, "input": in_name})

    info_obj = {
        "mode": "default",
        "judgeCaseMode": "default",
        "version": str(ts),
        "testCasesSize": len(test_cases),
        "testCases": info_cases,
    }

    info_path = os.path.join(problem_folder, "info")
    with open(info_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(info_obj, ensure_ascii=False, separators=(",", ":")))

    example_case = test_cases[0] if test_cases else {"input": "", "output": ""}
    examples = f"<input>{example_case.get('input', '')}</input><output>{example_case.get('output', '')}</output>"

    problem_json_obj = {
        "problem": {
            "stackLimit": 128,
            "modifiedUser": "root",
            "judgeCaseMode": "default",
            "auth": 1,
            "description": safe_text(description, ""),
            "source": "",
            "title": safe_text(title, "未命名题目"),
            "type": 0,
            "output": safe_text(output_spec, "请输出结果"),
            "ioScore": 100,
            "codeShare": True,
            "isFileIO": False,
            "isRemote": False,
            "timeLimit": 1000,
            "difficulty": 0,
            "input": safe_text(input_spec, "请读取输入"),
            "examples": examples,
            "hint": "",
            "isRemoveEndBlank": True,
            "openCaseResult": True,
            "memoryLimit": 256,
            "problemId": f"T{problem_num}",
            "isGroup": False,
            "isUploadCase": True,
            "judgeMode": "default",
        },
        "languages": LANGUAGES,
        "samples": samples,
        "tags": [],
        "codeTemplates": [],
        "judgeMode": "default",
    }

    problem_json_path = os.path.join(export_path, f"{problem_folder_name}.json")
    with open(problem_json_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(problem_json_obj, ensure_ascii=False, separators=(",", ":")))

    logger(f"导出目录已生成：{export_path}")

    zip_path = shutil.make_archive(export_path, "zip", root_dir=output_dir, base_dir=export_name)
    logger(f"ZIP 已生成：{zip_path}")

    return export_path, zip_path


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AI 题目样例生成器")
        self.root.geometry("1320x900")
        self.root.minsize(1100, 760)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.test_cases: List[Dict[str, str]] = []
        self.image_paths: List[str] = []
        self.pasted_image_dir = os.path.join(os.getcwd(), "pasted_images")

        self._build_ui()
        self.root.bind_all("<Control-Shift-V>", self._paste_image_from_clipboard)
        self.root.after(100, self._drain_log_queue)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        config_frame = ttk.LabelFrame(main, text="AI 配置")
        config_frame.pack(fill=tk.X, padx=2, pady=4)

        ttk.Label(config_frame, text="API URL").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.api_url_var = tk.StringVar(value="https://api.openai.com/v1")
        ttk.Entry(config_frame, textvariable=self.api_url_var).grid(row=0, column=1, columnspan=4, sticky="we", padx=6, pady=6)

        ttk.Label(config_frame, text="API Key").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        self.api_key_var = tk.StringVar()
        ttk.Entry(config_frame, textvariable=self.api_key_var, show="*").grid(row=1, column=1, columnspan=4, sticky="we", padx=6, pady=6)

        ttk.Label(config_frame, text="Model").grid(row=2, column=0, sticky="w", padx=6, pady=6)
        self.model_var = tk.StringVar(value="gpt-4o-mini")
        self.model_combo = ttk.Combobox(config_frame, textvariable=self.model_var, values=["gpt-4o-mini"])
        self.model_combo.grid(row=2, column=1, sticky="we", padx=6, pady=6)
        self.refresh_models_btn = ttk.Button(config_frame, text="拉取模型", command=self._refresh_models)
        self.refresh_models_btn.grid(row=2, column=2, sticky="w", padx=6, pady=6)
        ttk.Label(config_frame, text="流式请求: 已启用").grid(row=2, column=3, sticky="w", padx=6, pady=6)
        config_frame.columnconfigure(1, weight=3)
        config_frame.columnconfigure(4, weight=2)

        body_pane = ttk.Panedwindow(main, orient=tk.HORIZONTAL)
        body_pane.pack(fill=tk.BOTH, expand=True, padx=2, pady=4)

        left = ttk.Frame(body_pane)
        right = ttk.Frame(body_pane)
        body_pane.add(left, weight=3)
        body_pane.add(right, weight=2)

        problem_frame = ttk.LabelFrame(left, text="题目信息")
        problem_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        ttk.Label(problem_frame, text="标题").pack(anchor="w", padx=8, pady=(8, 0))
        self.title_var = tk.StringVar()
        ttk.Entry(problem_frame, textvariable=self.title_var).pack(fill=tk.X, padx=8, pady=4)

        ttk.Label(problem_frame, text="题目文字信息").pack(anchor="w", padx=8, pady=(6, 0))
        self.problem_text = tk.Text(problem_frame, height=16, wrap="word")
        self.problem_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        image_frame = ttk.LabelFrame(left, text="截图输入（可多选，可粘贴）")
        image_frame.pack(fill=tk.BOTH, expand=False)

        ttk.Label(
            image_frame,
            text="支持文件添加，或使用 Ctrl+Shift+V 粘贴剪贴板截图",
        ).pack(anchor="w", padx=8, pady=(8, 2))

        self.image_list = tk.Listbox(image_frame, height=8)
        self.image_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        image_btn_row = ttk.Frame(image_frame)
        image_btn_row.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(image_btn_row, text="添加截图", command=self._add_images).pack(side=tk.LEFT)
        ttk.Button(image_btn_row, text="粘贴截图", command=self._paste_image_from_clipboard).pack(side=tk.LEFT, padx=6)
        ttk.Button(image_btn_row, text="移除选中", command=self._remove_selected_image).pack(side=tk.LEFT, padx=6)
        ttk.Button(image_btn_row, text="清空截图", command=self._clear_images).pack(side=tk.LEFT)

        right_tabs = ttk.Notebook(right)
        right_tabs.pack(fill=tk.BOTH, expand=True)

        code_tab = ttk.Frame(right_tabs)
        case_tab = ttk.Frame(right_tabs)
        right_tabs.add(code_tab, text="代码")
        right_tabs.add(case_tab, text="测试用例")

        code_frame = ttk.LabelFrame(code_tab, text="正确代码（可空，空则自动生成）")
        code_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self.code_text = tk.Text(code_frame, height=20, wrap="none")
        self.code_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        case_frame = ttk.LabelFrame(case_tab, text="测试用例（可多组）")
        case_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        case_top = ttk.Frame(case_frame)
        case_top.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        in_box = ttk.Frame(case_top)
        out_box = ttk.Frame(case_top)
        in_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        out_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        ttk.Label(in_box, text="Input").pack(anchor="w")
        self.case_input_text = tk.Text(in_box, height=8, wrap="word")
        self.case_input_text.pack(fill=tk.BOTH, expand=True)

        ttk.Label(out_box, text="Expected Output（可空）").pack(anchor="w")
        self.case_output_text = tk.Text(out_box, height=8, wrap="word")
        self.case_output_text.pack(fill=tk.BOTH, expand=True)

        case_btns = ttk.Frame(case_frame)
        case_btns.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(case_btns, text="新增/更新当前用例", command=self._upsert_case).pack(side=tk.LEFT)
        ttk.Button(case_btns, text="删除选中用例", command=self._delete_case).pack(side=tk.LEFT, padx=6)
        ttk.Button(case_btns, text="清空用例", command=self._clear_cases).pack(side=tk.LEFT)

        self.case_list = tk.Listbox(case_frame, height=6)
        self.case_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.case_list.bind("<<ListboxSelect>>", self._on_case_select)

        output_frame = ttk.LabelFrame(main, text="导出配置")
        output_frame.pack(fill=tk.X, padx=2, pady=4)

        ttk.Label(output_frame, text="输出目录").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.output_dir_var = tk.StringVar(value=os.getcwd())
        ttk.Entry(output_frame, textvariable=self.output_dir_var).grid(row=0, column=1, sticky="we", padx=6, pady=6)
        ttk.Button(output_frame, text="选择目录", command=self._choose_output_dir).grid(row=0, column=2, padx=6, pady=6)
        output_frame.columnconfigure(1, weight=1)

        action_frame = ttk.Frame(main)
        action_frame.pack(fill=tk.X, padx=2, pady=4)
        self.run_btn = ttk.Button(action_frame, text="开始处理并导出", command=self._start)
        self.run_btn.pack(side=tk.LEFT)
        ttk.Button(action_frame, text="清空日志", command=self._clear_logs).pack(side=tk.LEFT, padx=8)

        log_frame = ttk.LabelFrame(main, text="运行日志")
        log_frame.pack(fill=tk.BOTH, expand=False, padx=2, pady=4)

        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {msg}")

    def _drain_log_queue(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, line + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")
        self.root.after(100, self._drain_log_queue)

    def _clear_logs(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def _set_code_text(self, code: str) -> None:
        self.code_text.delete("1.0", tk.END)
        self.code_text.insert("1.0", code)

    def _refresh_models(self) -> None:
        api_url = self.api_url_var.get().strip()
        if not api_url:
            messagebox.showerror("错误", "请先填写 API URL")
            return
        api_key = self.api_key_var.get().strip()
        self.refresh_models_btn.configure(state="disabled")
        self._log("开始拉取模型列表...")
        threading.Thread(target=self._refresh_models_worker, args=(api_url, api_key), daemon=True).start()

    def _refresh_models_worker(self, api_url: str, api_key: str) -> None:
        try:
            models = fetch_available_models(
                api_url=api_url,
                api_key=api_key,
            )
            self.root.after(0, lambda: self._apply_models(models))
            self._log(f"模型拉取成功，共 {len(models)} 个")
        except Exception as e:
            self._log(f"模型拉取失败：{e}")
            self.root.after(0, lambda: messagebox.showerror("拉取模型失败", str(e)))
        finally:
            self.root.after(0, lambda: self.refresh_models_btn.configure(state="normal"))

    def _apply_models(self, models: List[str]) -> None:
        self.model_combo["values"] = models
        cur = self.model_var.get().strip()
        if not cur or cur not in models:
            self.model_var.set(models[0])

    def _add_image_path(self, path: str) -> None:
        norm = os.path.abspath(path)
        if norm not in self.image_paths:
            self.image_paths.append(norm)
            self.image_list.insert(tk.END, norm)

    def _add_images(self) -> None:
        files = filedialog.askopenfilenames(
            title="选择截图",
            filetypes=[("Image Files", "*.png *.jpg *.jpeg *.bmp *.webp"), ("All", "*.*")],
        )
        for p in files:
            self._add_image_path(p)

    def _paste_image_from_clipboard(self, _event=None) -> str:
        if ImageGrab is None:
            self._log("未安装 pillow，无法粘贴截图")
            messagebox.showwarning("提示", "粘贴截图依赖 pillow，请先安装：pip install pillow")
            return "break"

        data = ImageGrab.grabclipboard()
        if data is None:
            self._log("剪贴板中没有可用图片")
            return "break"

        added = 0
        if isinstance(data, list):
            for p in data:
                if not isinstance(p, str):
                    continue
                ext = os.path.splitext(p)[1].lower()
                if ext in {".png", ".jpg", ".jpeg", ".bmp", ".webp"} and os.path.isfile(p):
                    self._add_image_path(p)
                    added += 1
        elif hasattr(data, "save"):
            os.makedirs(self.pasted_image_dir, exist_ok=True)
            out_path = os.path.join(self.pasted_image_dir, f"clipboard_{int(time.time() * 1000)}.png")
            data.save(out_path, "PNG")
            self._add_image_path(out_path)
            added += 1

        if added:
            self._log(f"已从剪贴板添加 {added} 张截图")
        else:
            self._log("剪贴板内容不是图片")
        return "break"

    def _remove_selected_image(self) -> None:
        sel = self.image_list.curselection()
        if not sel:
            return
        idx = sel[0]
        self.image_list.delete(idx)
        self.image_paths.pop(idx)

    def _clear_images(self) -> None:
        self.image_paths.clear()
        self.image_list.delete(0, tk.END)

    def _upsert_case(self) -> None:
        inp = self.case_input_text.get("1.0", tk.END).rstrip("\n")
        out = self.case_output_text.get("1.0", tk.END).rstrip("\n")

        if not inp.strip() and not out.strip():
            messagebox.showwarning("提示", "请至少填写输入或输出")
            return

        sel = self.case_list.curselection()
        if sel:
            idx = sel[0]
            self.test_cases[idx] = {"input": inp, "output": out}
            self._refresh_case_list(select_idx=idx)
        else:
            self.test_cases.append({"input": inp, "output": out})
            self._refresh_case_list(select_idx=len(self.test_cases) - 1)

    def _case_label(self, idx: int, inp: str, out: str) -> str:
        input_lines = inp.count("\n") + (1 if inp else 0)
        has_out = "有输出" if out.strip() else "无输出"
        return f"Case {idx + 1} | {input_lines} 行输入 | {has_out}"

    def _refresh_case_list(self, select_idx: Optional[int] = None) -> None:
        self.case_list.delete(0, tk.END)
        for idx, case in enumerate(self.test_cases):
            self.case_list.insert(
                tk.END,
                self._case_label(idx, case.get("input", ""), case.get("output", "")),
            )
        if select_idx is not None and 0 <= select_idx < len(self.test_cases):
            self.case_list.selection_set(select_idx)
            self.case_list.activate(select_idx)

    def _delete_case(self) -> None:
        sel = self.case_list.curselection()
        if not sel:
            return
        idx = sel[0]
        self.test_cases.pop(idx)
        next_idx = idx if idx < len(self.test_cases) else len(self.test_cases) - 1
        self._refresh_case_list(select_idx=next_idx if next_idx >= 0 else None)

    def _clear_cases(self) -> None:
        self.test_cases.clear()
        self._refresh_case_list()
        self.case_input_text.delete("1.0", tk.END)
        self.case_output_text.delete("1.0", tk.END)

    def _on_case_select(self, _event=None) -> None:
        sel = self.case_list.curselection()
        if not sel:
            return
        idx = sel[0]
        case = self.test_cases[idx]
        self.case_input_text.delete("1.0", tk.END)
        self.case_output_text.delete("1.0", tk.END)
        self.case_input_text.insert("1.0", case.get("input", ""))
        self.case_output_text.insert("1.0", case.get("output", ""))

    def _choose_output_dir(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_dir_var.set(path)

    def _collect_payload(self) -> Dict[str, Any]:
        return {
            "api_url": self.api_url_var.get().strip(),
            "api_key": self.api_key_var.get().strip(),
            "model": self.model_var.get().strip() or "gpt-4o-mini",
            "title": self.title_var.get().strip(),
            "problem_text": self.problem_text.get("1.0", tk.END).strip(),
            "image_paths": list(self.image_paths),
            "code": self.code_text.get("1.0", tk.END).strip(),
            "test_cases": [dict(c) for c in self.test_cases],
            "output_dir": self.output_dir_var.get().strip() or os.getcwd(),
        }

    def _build_stream_callback(self, stage: str):
        progress = {"chars": 0, "last_log": 0}

        def on_chunk(chunk: str) -> None:
            progress["chars"] += len(chunk)
            if progress["chars"] - progress["last_log"] >= 240:
                progress["last_log"] = progress["chars"]
                self._log(f"{stage}流式返回中... {progress['chars']} 字符")

        return on_chunk

    def _start(self) -> None:
        payload = self._collect_payload()
        if not payload["problem_text"] and not payload["image_paths"]:
            messagebox.showerror("错误", "请填写题目文字信息或上传至少一张截图")
            return

        if not os.path.isdir(payload["output_dir"]):
            messagebox.showerror("错误", "输出目录不存在")
            return

        self.run_btn.configure(state="disabled")
        self._log("任务开始")
        threading.Thread(target=self._worker, args=(payload,), daemon=True).start()

    def _worker(self, payload: Dict[str, Any]) -> None:
        try:
            export_path, zip_path = self._process(payload)
            self._log("全部完成")
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "完成",
                    f"导出目录：\n{export_path}\n\nZIP：\n{zip_path}",
                ),
            )
        except Exception as e:
            detail = traceback.format_exc()
            self._log(f"任务失败：{e}")
            self._log(detail)
            self.root.after(0, lambda: messagebox.showerror("失败", str(e)))
        finally:
            self.root.after(0, lambda: self.run_btn.configure(state="normal"))

    def _process(self, payload: Dict[str, Any]) -> Tuple[str, str]:
        api_url = normalize_chat_url(payload["api_url"])
        api_key = payload["api_key"]
        model = payload["model"]

        problem_text = payload["problem_text"]
        user_title = payload["title"]
        image_paths = payload["image_paths"]
        output_dir = payload["output_dir"]

        code = payload["code"]
        cases = payload["test_cases"]

        title = user_title or "未命名题目"
        description = problem_text
        input_spec = "请读取标准输入"
        output_spec = "请输出结果"

        if code:
            self._log("检测到已提供代码，跳过 AI 生成")
        else:
            if not api_url:
                raise ValueError("代码为空时，必须填写 API URL")

            generated = generate_code_with_ai(
                api_url=api_url,
                api_key=api_key,
                model=model,
                problem_text=problem_text,
                image_paths=image_paths,
                logger=self._log,
                stream_callback=self._build_stream_callback("代码生成"),
            )

            code = safe_text(generated.get("code"), "")
            if not code:
                raise RuntimeError("AI 返回中没有 code 字段")

            title = safe_text(user_title, safe_text(generated.get("title"), "未命名题目"))
            description = safe_text(generated.get("description"), problem_text)
            input_spec = safe_text(generated.get("input_spec"), "请读取标准输入")
            output_spec = safe_text(generated.get("output_spec"), "请输出结果")

            ai_cases = generated.get("test_cases") or []
            if not cases and isinstance(ai_cases, list):
                normalized_ai_cases = []
                for c in ai_cases:
                    if isinstance(c, dict):
                        normalized_ai_cases.append(
                            {
                                "input": str(c.get("input", "")),
                                "output": str(c.get("output", "")),
                            }
                        )
                cases = normalized_ai_cases
                self._log(f"使用 AI 生成测试用例 {len(cases)} 组")

        if not cases:
            raise ValueError("没有可用测试用例，请至少添加一组测试，或让 AI 返回 test_cases")

        if not code.strip():
            raise ValueError("最终代码为空，无法执行")

        can_repair_with_ai = bool(api_url)
        max_attempts = 3 if can_repair_with_ai else 1
        if payload["code"].strip() and can_repair_with_ai:
            self._log("检测到手动代码：若校验失败将自动调用 AI 修复")
        elif payload["code"].strip() and not can_repair_with_ai:
            self._log("检测到手动代码：未配置 API，仅执行一次校验")

        final_cases = cases

        for attempt in range(1, max_attempts + 1):
            self._log(f"开始执行校验，尝试 {attempt}/{max_attempts}")
            passed, details, adjusted_cases = run_and_validate(code, final_cases, self._log)
            final_cases = adjusted_cases

            if passed:
                self._log("代码校验通过")
                break

            if attempt < max_attempts:
                failure_report = build_failure_report(details)
                repaired = repair_code_with_ai(
                    api_url=api_url,
                    api_key=api_key,
                    model=model,
                    problem_text=problem_text,
                    previous_code=code,
                    failure_report=failure_report,
                    test_cases=final_cases,
                    logger=self._log,
                    stream_callback=self._build_stream_callback("代码修复"),
                )
                new_code = safe_text(repaired.get("code"), "")
                if not new_code:
                    raise RuntimeError("AI 修复返回为空代码")
                code = new_code
                self._log("已获取修复后的代码，继续校验")
            else:
                fail_count = sum(1 for d in details if not d.get("passed"))
                if not can_repair_with_ai:
                    raise RuntimeError(
                        f"校验失败：仍有 {fail_count} 组测试未通过。"
                        "如需自动修复，请配置 API URL/Key 后重试。"
                    )
                raise RuntimeError(f"校验失败：仍有 {fail_count} 组测试未通过")

        export_path, zip_path = create_problem_export(
            output_dir=output_dir,
            title=title,
            description=description,
            input_spec=input_spec,
            output_spec=output_spec,
            test_cases=final_cases,
            logger=self._log,
        )

        self.root.after(0, lambda: self._set_code_text(code))
        return export_path, zip_path


def main() -> None:
    root = tk.Tk()
    app = App(root)
    app._log("应用启动完成")
    root.mainloop()


if __name__ == "__main__":
    main()
