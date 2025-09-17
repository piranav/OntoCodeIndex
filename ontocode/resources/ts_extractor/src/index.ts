#!/usr/bin/env node
import { accessSync } from "node:fs";
import path from "node:path";
import process from "node:process";

import fg from "fast-glob";
import { Project, ts } from "ts-morph";

import { collectFacts } from "./collect.js";

interface CliOptions {
  repo: string;
  include: string[];
  exclude: string[];
}

function parseArgs(argv: string[]): CliOptions {
  const opts: CliOptions = {
    repo: process.cwd(),
    include: ["**/*.{ts,tsx,js,jsx}"],
    exclude: ["node_modules/**", ".next/**", "dist/**"],
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--repo" && argv[i + 1]) {
      opts.repo = argv[i + 1];
      i += 1;
    } else if (arg === "--files-include" && argv[i + 1]) {
      opts.include = argv[i + 1]
        .split(",")
        .map((p) => p.trim())
        .filter(Boolean);
      i += 1;
    } else if (arg === "--files-exclude" && argv[i + 1]) {
      const extra = argv[i + 1]
        .split(",")
        .map((p) => p.trim())
        .filter(Boolean);
      opts.exclude = [...opts.exclude, ...extra];
      i += 1;
    }
  }
  return opts;
}

function detectTsConfig(repo: string): string | undefined {
  for (const file of ["tsconfig.json", "jsconfig.json"]) {
    const location = path.join(repo, file);
    try {
      accessSync(location);
      return location;
    } catch {
      continue;
    }
  }
  return undefined;
}

async function main(): Promise<void> {
  const opts = parseArgs(process.argv.slice(2));
  const repo = path.resolve(process.cwd(), opts.repo);
  const tsconfig = detectTsConfig(repo);
  const project = tsconfig
    ? new Project({ tsConfigFilePath: tsconfig })
    : new Project({
        compilerOptions: {
          module: ts.ModuleKind.ESNext,
          target: ts.ScriptTarget.ES2022,
          moduleResolution: ts.ModuleResolutionKind.NodeJs,
          jsx: ts.JsxEmit.ReactJSX,
          allowJs: true,
          esModuleInterop: true,
          resolveJsonModule: true,
          skipLibCheck: true,
        },
      });
  project.addSourceFilesAtPaths(opts.include.map((pattern) => path.join(repo, pattern)));
  const envList = process.env.ONTOCODE_FILE_LIST;
  let files: string[];
  if (envList) {
    try {
      const parsed = JSON.parse(envList) as string[];
      files = parsed.map((rel) => path.resolve(repo, rel));
    } catch (error) {
      console.error("Invalid ONTOCODE_FILE_LIST", error);
      process.exit(1);
      return;
    }
  } else {
    files = await fg(opts.include, {
      cwd: repo,
      ignore: opts.exclude,
      absolute: true,
      suppressErrors: true,
      followSymbolicLinks: true,
    });
  }
  const seen = new Set<string>();
  for (const file of files) {
    if (seen.has(file)) {
      continue;
    }
    seen.add(file);
    const source = project.getSourceFile(file) ?? project.addSourceFileAtPathIfExists(file);
    if (!source) {
      continue;
    }
    const facts = collectFacts(project, source);
    facts.filePath = path.relative(repo, file).replace(/\\/g, "/");
    process.stdout.write(`${JSON.stringify(facts)}\n`);
  }
}

main().catch((error) => {
  console.error("[ontocode-ts-extractor]", error);
  process.exitCode = 1;
});
