#!/usr/bin/env node
// memoloupe-skill 安装器：把 SKILL.md 复制到 agent skills 目录。
// 用法：
//   npx memoloupe-skill            # 安装到 ~/.agents/skills/memoloupe
//   npx memoloupe-skill --target ~/.claude/skills/memoloupe
//   npx memoloupe-skill --force    # 覆盖已有安装
import { copyFileSync, mkdirSync, existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const pkgRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);

function argValue(flag) {
  const i = args.indexOf(flag);
  return i >= 0 ? args[i + 1] : null;
}

if (args.includes("--help") || args.includes("-h")) {
  console.log(
    [
      "memoloupe-skill：安装 MemoLoupe agent skill",
      "",
      "用法：npx memoloupe-skill [--target <dir>] [--force]",
      "",
      "默认目标：~/.agents/skills/memoloupe",
    ].join("\n"),
  );
  process.exit(0);
}

const target = resolve(
  argValue("--target") || join(process.env.HOME ?? "", ".agents", "skills", "memoloupe"),
);
const force = args.includes("--force");

if (existsSync(join(target, "SKILL.md")) && !force) {
  console.error(`已存在于 ${target}；覆盖请加 --force`);
  process.exit(1);
}

mkdirSync(target, { recursive: true });
copyFileSync(join(pkgRoot, "SKILL.md"), join(target, "SKILL.md"));

const pkg = JSON.parse(readFileSync(join(pkgRoot, "package.json"), "utf8"));
console.log(`✓ memoloupe-skill v${pkg.version} 已安装到 ${target}`);
console.log("  重启 agent 会话后生效；确认 memoloupe CLI：memoloupe --help");
