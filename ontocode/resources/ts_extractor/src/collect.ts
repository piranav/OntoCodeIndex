import path from "node:path";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import type {
  FunctionLikeDeclaration,
  Node,
  Project,
  SourceFile,
  Symbol as TsSymbol,
  TypeChecker,
} from "ts-morph";
import {
  Node as TsNode,
  SyntaxKind,
  ts,
} from "ts-morph";

export interface Span {
  startLine: number;
  startCol: number;
  endLine: number;
  endCol: number;
}

export interface UnitRecord {
  kind: "callable" | "classifier" | "variable" | "parameter" | "type";
  name: string;
  qualifiedName: string;
  symbolId: string;
  span: Span;
  astPath: string;
  isExportedDefault?: boolean;
  isAsync?: boolean;
}

export interface ImportRecord {
  from: string;
  resolvedKind: "package" | "file" | "unknown";
  resolved?: string;
}

export interface ExportRecord {
  name: string;
  unitSymbolId?: string;
}

export interface OccurrenceRecord {
  relation: "calls" | "references" | "reads" | "writes";
  subjectSymbolId: string;
  objectSymbolId?: string;
  objectQName?: string;
  span: Span;
}

export interface FileFacts {
  filePath: string;
  sha256: string;
  units: UnitRecord[];
  imports: ImportRecord[];
  exports: ExportRecord[];
  occurrences: OccurrenceRecord[];
  extends: Array<{ subject: string; target: string }>;
  implements: Array<{ subject: string; target: string }>;
  hasUseClientDirective: boolean;
}

const BASE64_MAP: Record<string, string> = {
  "+": "-",
  "/": "_",
  "=": "",
};

function base64Url(value: string): string {
  return Buffer.from(value).toString("base64").replace(/[+/=]/g, (m) => BASE64_MAP[m]);
}

export function symbolId(kind: string, qualifiedName: string): string {
  return base64Url(`ts:${kind}:${qualifiedName}`);
}

function moduleQualifiedName(relativePath: string): string {
  const withoutExt = relativePath.replace(/\.[^.]+$/, "");
  return withoutExt.replace(/[\\/]+/g, ".");
}

function spanFromNode(node: Node): Span {
  const source = node.getSourceFile();
  const start = source.getLineAndColumnAtPos(node.getNonWhitespaceStart());
  const end = source.getLineAndColumnAtPos(node.getEnd());
  return {
    startLine: start.line,
    startCol: start.column,
    endLine: end.line,
    endCol: end.column,
  };
}

function astPath(node: Node): string {
  const pieces: string[] = [];
  let current: Node | undefined = node;
  while (current) {
    const parent = current.getParent();
    if (!parent) {
      break;
    }
    const siblings = parent.getChildrenOfKind(current.getKind());
    const index = siblings.indexOf(current as never);
    pieces.push(`${current.getKindName()}[${index}]`);
    current = parent;
  }
  return pieces.reverse().join("/");
}

function exportDefaultSymbolId(sourceFile: SourceFile, checker: TypeChecker): string | undefined {
  const defaultExport = sourceFile.getDefaultExportSymbol();
  if (!defaultExport) {
    return undefined;
  }
  const declarations = defaultExport.getDeclarations();
  const decl = declarations[0];
  if (!decl) {
    return undefined;
  }
  const qn = buildQualifiedName(sourceFile, decl as Node, checker) ?? undefined;
  if (!qn) {
    return undefined;
  }
  const kind = inferUnitKind(decl as Node);
  return symbolId(kind, qn);
}

function inferUnitKind(node: Node): UnitRecord["kind"] {
  switch (node.getKind()) {
    case SyntaxKind.FunctionDeclaration:
    case SyntaxKind.MethodDeclaration:
    case SyntaxKind.FunctionExpression:
    case SyntaxKind.ArrowFunction:
    case SyntaxKind.Constructor:
    case SyntaxKind.GetAccessor:
    case SyntaxKind.SetAccessor:
      return "callable";
    case SyntaxKind.ClassDeclaration:
    case SyntaxKind.InterfaceDeclaration:
    case SyntaxKind.EnumDeclaration:
      return "classifier";
    case SyntaxKind.VariableDeclaration:
      return "variable";
    case SyntaxKind.Parameter:
      return "parameter";
    default:
      return "type";
  }
}

function projectRoot(sourceFile: SourceFile): string {
  return (
    process.env.ONTOCODE_EXTRACTOR_REPO
    ?? sourceFile.getProject().getCompilerOptions().rootDir
    ?? process.cwd()
  );
}

function buildQualifiedName(sourceFile: SourceFile, node: Node, checker: TypeChecker): string | undefined {
  const baseRoot = projectRoot(sourceFile);
  const relPath = path.relative(baseRoot, sourceFile.getFilePath()).replace(/\\/g, "/");
  const moduleName = moduleQualifiedName(relPath);
  const symbol = (node as Node & { getSymbol?: () => TsSymbol | undefined }).getSymbol?.();
  const baseName = symbol?.getName() ?? inferNodeName(node);
  if (!baseName) {
    return undefined;
  }
  return `${moduleName}.${baseName}`;
}

function inferNodeName(node: Node): string | undefined {
  if (TsNode.isFunctionDeclaration(node)) {
    const name = node.getName();
    if (name) {
      return name;
    }
    const hasDefaultModifier = node
      .getModifiers()
      .some((modifier) => modifier.getKind() === ts.SyntaxKind.DefaultKeyword);
    if (hasDefaultModifier) {
      return "default";
    }
  }
  if (TsNode.isVariableDeclaration(node)) {
    return node.getNameNode().getText();
  }
  if (TsNode.isClassDeclaration(node) || TsNode.isInterfaceDeclaration(node) || TsNode.isEnumDeclaration(node)) {
    const name = node.getName();
    if (name) {
      return name;
    }
  }
  if (TsNode.isMethodDeclaration(node) || TsNode.isGetAccessorDeclaration(node) || TsNode.isSetAccessorDeclaration(node)) {
    const nameNode = node.getNameNode?.();
    if (nameNode) {
      return nameNode.getText();
    }
  }
  if (TsNode.isArrowFunction(node)) {
    return "arrow";
  }
  if (TsNode.isFunctionExpression(node)) {
    return "anonymous";
  }
  return undefined;
}

function collectImportRecords(sourceFile: SourceFile): ImportRecord[] {
  const baseRoot = projectRoot(sourceFile);
  return sourceFile.getImportDeclarations().map((decl) => {
    const spec = decl.getModuleSpecifierValue();
    const target = decl.getModuleSpecifierSourceFile();
    if (target) {
      const rel = path.relative(baseRoot, target.getFilePath()).replace(/\\/g, "/");
      return { from: spec, resolvedKind: "file", resolved: rel };
    }
    return { from: spec, resolvedKind: spec.startsWith(".") ? "unknown" : "package" };
  });
}

function collectExportRecords(sourceFile: SourceFile, checker: TypeChecker, moduleName: string): ExportRecord[] {
  const exports: ExportRecord[] = [];
  for (const sym of sourceFile.getExportSymbols()) {
    const decl = sym.getDeclarations()[0];
    if (!decl) {
      continue;
    }
    const kind = inferUnitKind(decl as Node);
    const qn = buildQualifiedName(sourceFile, decl as Node, checker);
    const id = qn ? symbolId(kind, qn) : undefined;
    exports.push({ name: sym.getName(), unitSymbolId: id });
  }
  const defaultId = exportDefaultSymbolId(sourceFile, checker);
  if (defaultId) {
    exports.push({ name: "default", unitSymbolId: defaultId });
  }
  return exports;
}

function collectCallOccurrences(
  container: FunctionLikeDeclaration,
  subjectId: string,
  checker: TypeChecker
): OccurrenceRecord[] {
  const occurrences: OccurrenceRecord[] = [];
  container.forEachDescendant((node) => {
    if (!Node.isCallExpression(node)) {
      return;
    }
    const expression = node.getExpression();
    const symbol = expression.getSymbol() ?? checker.getSymbolAtLocation(expression.compilerNode);
    const qn = symbol ? checker.getFullyQualifiedName(symbol) : expression.getText();
    const objectQName = qn.replace(/["']/g, "").replace(/\//g, ".");
    const span = spanFromNode(node);
    const objectDecl = symbol?.getDeclarations()?.[0];
    let objectSymbolId: string | undefined;
    if (objectDecl) {
      const unitKind = inferUnitKind(objectDecl as Node);
      const objectQn = buildQualifiedName(objectDecl.getSourceFile(), objectDecl as Node, checker);
      if (objectQn) {
        objectSymbolId = symbolId(unitKind, objectQn);
      }
    }
    occurrences.push({
      relation: "calls",
      subjectSymbolId: subjectId,
      objectSymbolId,
      objectQName,
      span,
    });
  });
  return occurrences;
}

function collectUnits(sourceFile: SourceFile, checker: TypeChecker): { units: UnitRecord[]; occurrences: OccurrenceRecord[] } {
  const units: UnitRecord[] = [];
  const occurrences: OccurrenceRecord[] = [];
  sourceFile.forEachDescendant((node) => {
    if (
      !Node.isFunctionDeclaration(node) &&
      !Node.isFunctionExpression(node) &&
      !Node.isArrowFunction(node) &&
      !Node.isMethodDeclaration(node) &&
      !Node.isClassDeclaration(node) &&
      !Node.isInterfaceDeclaration(node) &&
      !Node.isEnumDeclaration(node)
    ) {
      return;
    }
    const qn = buildQualifiedName(sourceFile, node, checker);
    if (!qn) {
      return;
    }
    const kind = inferUnitKind(node);
    const id = symbolId(kind, qn);
    const span = spanFromNode(node);
    const unit: UnitRecord = {
      kind,
      name: inferNodeName(node) ?? node.getKindName(),
      qualifiedName: qn,
      symbolId: id,
      span,
      astPath: astPath(node),
      isExportedDefault: node.hasModifier?.(ts.SyntaxKind.DefaultKeyword) ?? false,
      isAsync: (node as FunctionLikeDeclaration).isAsync?.() ?? false,
    };
    const isDefault = node.getSourceFile().getDefaultExportSymbol()?.getDeclarations()?.includes(node) ?? false;
    if (isDefault) {
      unit.isExportedDefault = true;
    }
    units.push(unit);
    if (Node.isFunctionDeclaration(node) || Node.isMethodDeclaration(node) || Node.isArrowFunction(node) || Node.isFunctionExpression(node)) {
      occurrences.push(...collectCallOccurrences(node as FunctionLikeDeclaration, id, checker));
    }
  });
  return { units, occurrences };
}

function hasUseClientDirective(sourceFile: SourceFile): boolean {
  const statements = sourceFile.getStatements();
  for (const stmt of statements) {
    if (!Node.isExpressionStatement(stmt)) {
      break;
    }
    const expression = stmt.getExpression();
    if (!Node.isStringLiteral(expression)) {
      break;
    }
    if (expression.getLiteralText() === "use client") {
      return true;
    }
  }
  return false;
}

export function collectFacts(project: Project, sourceFile: SourceFile): FileFacts {
  const relPath = sourceFile.getFilePath();
  const checker = project.getTypeChecker();
  const moduleName = moduleQualifiedName(relPath);
  const { units, occurrences } = collectUnits(sourceFile, checker);
  const facts: FileFacts = {
    filePath: relPath,
    sha256: createHash("sha256").update(readFileSync(sourceFile.getFilePath())).digest("hex"),
    units,
    imports: collectImportRecords(sourceFile),
    exports: collectExportRecords(sourceFile, checker, moduleName),
    occurrences,
    extends: [],
    implements: [],
    hasUseClientDirective: hasUseClientDirective(sourceFile),
  };
  return facts;
}
