# AI 题目样例生成器

一个带 UI 的本地应用，支持：

- 对接自定义 AI URL + API Key + Model
- 使用 OpenAI 标准接口，AI 调用采用流式请求（stream）
- 软件内拉取模型列表并下拉选择
- 输入题目文字信息
- 多截图输入（可多选上传）
- 支持粘贴截图（`Ctrl+Shift+V`）
- 单测超时秒数可配置（默认 15 秒）
- 可手动输入正确代码，也可留空让 AI 自动生成
- 自动运行代码并测试校验
- 手动代码若校验失败且已配置 API，会自动进入 AI 修复重试
- 按 `problem_export_<timestamp>` 格式导出，并自动打包 zip
- 校验时会把标准代码写入独立 `standard_solution.py` 后再执行

## 运行方式

```bash
python ai_problem_generator.py
```

> 需要 Python 3.9+（Tkinter 通常随 Python 自带）。
>
> 如需“粘贴截图”功能，请安装：`pip install pillow`

## 使用流程

1. 在 `AI 配置` 中填写：
   - `API URL`：填写 OpenAI 标准根地址（如 `https://api.openai.com/v1`），也兼容直接填 `chat/completions` 地址
   - `API Key`：可空（如果你的网关不需要）
   - `Model`：可手动填写，也可点击“拉取模型”后下拉选择
   - `单测超时(秒)`：建议 10-30，题目复杂可调大
2. 在 `题目信息` 中填写题目文本，可上传多张截图。
   - 支持 `Ctrl+Shift+V` 直接粘贴截图到列表
3. `正确代码` 区域：
   - 如果你有代码，直接粘贴；
   - 如果留空，程序会调用 AI 自动生成 Python3 代码。
4. 添加测试用例（可多组）：
   - `Input` 必填建议；
   - `Expected Output` 可空（为空时会使用程序实测输出作为导出输出）。
5. 点击 `开始处理并导出`。
6. 在 `运行日志` 查看全过程，完成后会弹窗显示：
   - 导出目录路径
   - zip 文件路径

## 校验逻辑

- 始终会执行代码跑测试。
- 若用例有期望输出：进行比对校验。
- 若用例无期望输出：记录实测输出写入 `.out` 文件。
- 当代码由 AI 生成且校验失败时，会最多自动修复重试 3 次。

## 导出结构

会生成如下结构（示例）：

```text
problem_export_1777083194814/
  problem_12345.json
  problem_12345/
    info
    1.in
    1.out
    2.in
    2.out
```

并同时生成：

```text
problem_export_1777083194814.zip
```

## 说明

- 当前执行器为 Python3。
- 导出的 JSON 和 `info` 文件字段已按你给的样例格式构建。

## GitHub Actions 自动构建

- 已提供工作流：[build-executables.yml](.github/workflows/build-executables.yml)
- 触发时机：仅在你发布 Release 时触发（`release: published`）
- 构建平台：`Windows`、`macOS`、`Linux`
- 构建工具：`PyInstaller --onefile --windowed`
- 产物位置：直接上传到该次 GitHub Release 的 Assets

每个平台产物是一个 zip，内含对应可执行文件和 `README.md`。
