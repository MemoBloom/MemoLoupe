#!/usr/bin/env python3
"""生成 Homebrew tap 仓库骨架（dist/homebrew-tap/，可直接 git push）。

前置条件：目标版本 tag 已推送且 Release 工作流已产出 sdist 资产
（.github/workflows/release.yml）。脚本下载该资产计算真实 sha256，
渲染 Formula/memoloupe.rb 与 tap README。

用法：
    python scripts/gen_homebrew_tap.py --version v0.1.0

之后（用户创建 MemoBloom/homebrew-memoloupe 空仓库后）：
    cd dist/homebrew-tap
    git init && git add -A && git commit -m "memoloupe 0.1.0"
    git remote add origin git@github.com:MemoBloom/homebrew-memoloupe.git
    git push -u origin main
"""

from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path

FORMULA_TEMPLATE = '''class Memoloupe < Formula
  include Language::Python::Virtualenv
  include Language::Python::Shebang

  desc "Staged reference-video analysis CLI (shot / story / style profile)"
  homepage "https://github.com/{repo}"
{url_lines}

  depends_on "python@3.12"
  depends_on "ffmpeg" # ffprobe/ffmpeg：切镜、波形、clip、审片索引

  # PyPI 依赖下限与 pyproject.toml 保持一致
  PYPI_DEPS = %w[numpy>=2.5.2 jsonschema>=4.23].freeze

  def install
    venv = virtualenv_create(libexec, "python3.12")
    host_python = Formula["python@3.12"].opt_bin/"python3.12"
    # 本体：不解析依赖（Homebrew 6 的 venv 无 pip，用宿主 pip --python）
    system host_python, "-m", "pip", "--python=#{{libexec}}/bin/python",
                        "install", "--no-deps", "-v", buildpath
    # 运行时依赖：从 PyPI 解析安装（numpy 命中预编译 wheel）。
    # 个人 tap 权衡：非完全 hermetic（未用 resources 全锁定），见 README。
    system host_python, "-m", "pip", "--python=#{{libexec}}/bin/python",
                        "install", "-v", *PYPI_DEPS
    bin.install_symlink Dir[libexec/"bin/*"]
  end

  test do
    assert_match "memoloupe", shell_output("#{{bin}}/memoloupe --help")
  end
end
'''

RELEASE_URL_LINES = '''  url "https://github.com/{repo}/releases/download/{tag}/memoloupe-{plain_version}.tar.gz"
  sha256 "{sha256}"'''

GIT_URL_LINES = '''  url "git@github.com:{repo}.git", using: :git, tag: "{tag}"
  version "{plain_version}"'''

GIT_URL_NOTE = (
    "git 策略：源仓库私有时，brew 经 SSH 克隆（需本机 ssh key 有仓库权限）；"
    "仓库转 public 后建议用 --strategy release 切换为 Release 资产 + sha256 校验。"
)

TAP_README_TEMPLATE = """# homebrew-memoloupe

MemoLoupe 拉片分析 CLI 的 Homebrew tap。

## 前提：仓库可见性

Formula 的下载源是 GitHub Release 资产；**brew 安装时不携带 GitHub
凭据**。若 MemoLoupe 仓库为 private，需要二选一：

1. 把 MemoLoupe 仓库改为 public；或
2. 建一个公开的发布仓库（如 `MemoBloom/memoloupe-releases`），Release
   工作流改为把资产发布到该仓库，并用
   `--repo MemoBloom/memoloupe-releases` 重新生成 formula。

## 安装

```bash
brew install memobloom/memoloupe/memoloupe
```

首次安装会创建独立 virtualenv 并从 PyPI 解析依赖（numpy/jsonschema），
需要网络；ffmpeg 由 Homebrew 自动带入。

## 前置配置（模型服务）

```bash
memoloupe connect add mimo   # 或 qwen；交互式录入 baseUrl/model/apiKey
memoloupe connect switch mimo
memoloupe connect status
```

未配置模型服务时，语义阶段显式降级（unavailable/skipped），确定性
阶段（切镜/音频/审片索引/切点指标）完全可用。

## 升级版本

1. MemoLoupe 仓库：bump `pyproject.toml` version → 推送 tag（如 `v0.1.1`）；
2. 等 Release 工作流产出 `memoloupe-0.1.1.tar.gz` 资产；
3. 在 MemoLoupe 仓库运行 `python scripts/gen_homebrew_tap.py --version v0.1.1`
   重新生成本仓库内容，替换 `Formula/memoloupe.rb` 后提交推送；
4. `brew upgrade memobloom/memoloupe/memoloupe`。

## 已知权衡

- 依赖在安装时从 PyPI 解析（未锁版本），非 Homebrew 标准的
  resources 全锁定模式；个人/组织 tap 可接受。
- Apple Vision 运动分析仅在 macOS 可用（Vision framework）。
- 本地 ASR（FireRedVAD + MLX Whisper）需要 Python extra
  `asr-local`，brew 安装默认不包含；ASR 自动走已配置的远程 provider。
"""


def sha256_of(url: str, *, tag: str, repo: str, plain_version: str) -> str:
    """下载资产并计算 sha256；私有仓库 404 时回退 gh CLI（认证下载）。"""
    import subprocess

    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url) as resp:  # noqa: S310 - 固定 GitHub 源
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    # 私有仓库：资产 URL 未认证访问 404 → 用 gh 的凭据下载到内存流。
    result = subprocess.run(
        ["gh", "release", "download", tag, "--repo", repo,
         "--pattern", f"memoloupe-{plain_version}.tar.gz", "-O", "-"],
        check=True, capture_output=True,
    )
    digest.update(result.stdout)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="tag 名，如 v0.1.1")
    parser.add_argument("--repo", default="MemoBloom/MemoLoupe")
    parser.add_argument("--out", default="dist/homebrew-tap")
    parser.add_argument(
        "--strategy",
        choices=["release", "git"],
        default="git",
        help="release=Release 资产 + sha256（要求源仓库 public）；"
        "git=SSH 克隆（私有源可用，无 sha256 校验）",
    )
    args = parser.parse_args()

    tag = args.version
    plain = tag.removeprefix("v")
    asset = f"https://github.com/{args.repo}/releases/download/{tag}/memoloupe-{plain}.tar.gz"

    if args.strategy == "release":
        print(f"下载并计算 sha256：{asset}")
        digest = sha256_of(asset, tag=tag, repo=args.repo, plain_version=plain)
        url_lines = RELEASE_URL_LINES.format(
            repo=args.repo, tag=tag, plain_version=plain, sha256=digest
        )
    else:
        digest = "（git 策略，无 sha256）"
        url_lines = GIT_URL_LINES.format(repo=args.repo, tag=tag, plain_version=plain)

    out = Path(args.out)
    (out / "Formula").mkdir(parents=True, exist_ok=True)
    formula = FORMULA_TEMPLATE.format(
        repo=args.repo, tag=tag, plain_version=plain, url_lines=url_lines
    )
    (out / "Formula" / "memoloupe.rb").write_text(formula, encoding="utf-8")
    readme = TAP_README_TEMPLATE
    if args.strategy == "git":
        readme += f"\n> 当前 formula 使用 git（SSH）策略：{GIT_URL_NOTE}\n"
    (out / "README.md").write_text(readme, encoding="utf-8")
    print(f"tap 内容已生成：{out}（strategy={args.strategy}）")
    if args.strategy == "release":
        print(f"  sha256({plain}) = {digest}")


if __name__ == "__main__":
    main()
