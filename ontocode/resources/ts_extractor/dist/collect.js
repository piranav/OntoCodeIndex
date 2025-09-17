import path from "node:path";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { Node as TsNode, SyntaxKind, ts } from "ts-morph";

const BASE64_MAP = {
  "+": "-",
  "/": "_",
  "=": "",
};

function base64Url(value) {
  return Buffer.from(value).toString("base64").replace(/[+/=]/g, (m) => BASE64_MAP[m]);
}

export function symbolId(kind, qualifiedName) {
  return base64Url(`ts:${kind}:${qualifiedName}`);
}

function moduleQualifiedName(relativePath) {
  const withoutExt = relativePath.replace(/\.[^.]+$/, "");
  return withoutExt.replace(/[\\/]+/g, ".");
}

function spanFromNode(node) {
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

function astPath(node) {
  const pieces = [];
  let current = node;
  while (current) {
    const parent = current.getParent();
    if (!parent) {
      break;
    }
    const siblings = parent.getChildrenOfKind(current.getKind());
    const index = siblings.indexOf(current);
    pieces.push(`${current.getKindName()}[${index}]`);
    current = parent;
  }
  return pieces.reverse().join("/");
}

function exportDefaultSymbolId(sourceFile, checker) {
  const defaultExport = sourceFile.getDefaultExportSymbol();
  if (!defaultExport) {
    return undefined;
  }
  const declarations = defaultExport.getDeclarations();
  const decl = declarations[0];
  if (!decl) {
    return undefined;
  }
  const qn = buildQualifiedName(sourceFile, decl, checker) ?? undefined;
  if (!qn) {
    return undefined;
  }
  const kind = inferUnitKind(decl);
  return symbolId(kind, qn);
}

function inferUnitKind(node) {
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

function projectRoot(sourceFile) {
  const project = sourceFile.getProject();
  return (process.env.ONTOCODE_EXTRACTOR_REPO
    ?? project.getCompilerOptions().rootDir
    ?? process.cwd());
}

function buildQualifiedName(sourceFile, node, checker) {
  const baseRoot = projectRoot(sourceFile);
  const relPath = path.relative(baseRoot, sourceFile.getFilePath()).replace(/\\/g, "/");
  const moduleName = moduleQualifiedName(relPath);
  const symbol = node.getSymbol?.();
  const baseName = symbol?.getName() ?? inferNodeName(node);
  if (!baseName) {
    return undefined;
  }
  return `${moduleName}.${baseName}`;
}

function inferNodeName(node) {
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

function collectImportRecords(sourceFile) {
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

function collectExportRecords(sourceFile, checker) {
  const exports = [];
  for (const sym of sourceFile.getExportSymbols()) {
    const decl = sym.getDeclarations()[0];
    if (!decl) {
      continue;
    }
    const kind = inferUnitKind(decl);
    const qn = buildQualifiedName(sourceFile, decl, checker);
    const id = qn ? symbolId(kind, qn) : undefined;
    exports.push({ name: sym.getName(), unitSymbolId: id });
  }
  const defaultId = exportDefaultSymbolId(sourceFile, checker);
  if (defaultId) {
    exports.push({ name: "default", unitSymbolId: defaultId });
  }
  return exports;
}

function collectCallOccurrences(container, subjectId, checker) {
  const occurrences = [];
  container.forEachDescendant((node) => {
    if (!node.getKind || node.getKind() !== SyntaxKind.CallExpression) {
      return;
    }
    const expression = node.getExpression();
    const symbol = expression.getSymbol?.() ?? checker.getSymbolAtLocation(expression.compilerNode);
    const qn = symbol ? checker.getFullyQualifiedName(symbol) : expression.getText();
    const objectQName = qn.replace(/["']/g, "").replace(/\//g, ".");
    const span = spanFromNode(node);
    const objectDecl = symbol?.getDeclarations?.()?.[0];
    let objectSymbolId;
    if (objectDecl) {
      const unitKind = inferUnitKind(objectDecl);
      const objectQn = buildQualifiedName(objectDecl.getSourceFile(), objectDecl, checker);
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

function collectUnits(sourceFile, checker) {
  const units = [];
  const occurrences = [];
  sourceFile.forEachDescendant((node) => {
    if (
      !node.getKind ||
      (
        node.getKind() !== SyntaxKind.FunctionDeclaration &&
        node.getKind() !== SyntaxKind.FunctionExpression &&
        node.getKind() !== SyntaxKind.ArrowFunction &&
        node.getKind() !== SyntaxKind.MethodDeclaration &&
        node.getKind() !== SyntaxKind.ClassDeclaration &&
        node.getKind() !== SyntaxKind.InterfaceDeclaration &&
        node.getKind() !== SyntaxKind.EnumDeclaration
      )
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
    const unit = {
      kind,
      name: inferNodeName(node) ?? node.getKindName?.() ?? "unknown",
      qualifiedName: qn,
      symbolId: id,
      span,
      astPath: astPath(node),
      isExportedDefault: node.hasModifier?.(ts.SyntaxKind.DefaultKeyword) ?? false,
      isAsync: node.isAsync?.() ?? false,
    };
    const defaultDecls = sourceFile.getDefaultExportSymbol?.()?.getDeclarations?.() ?? [];
    if (defaultDecls.includes(node)) {
      unit.isExportedDefault = true;
    }
    units.push(unit);
    if (
      node.getKind() === SyntaxKind.FunctionDeclaration ||
      node.getKind() === SyntaxKind.FunctionExpression ||
      node.getKind() === SyntaxKind.ArrowFunction ||
      node.getKind() === SyntaxKind.MethodDeclaration
    ) {
      occurrences.push(...collectCallOccurrences(node, id, checker));
    }
  });
  return { units, occurrences };
}

function hasUseClientDirective(sourceFile) {
  const statements = sourceFile.getStatements();
  for (const stmt of statements) {
    if (!stmt.getExpression) {
      break;
    }
    if (stmt.getKind() !== SyntaxKind.ExpressionStatement) {
      break;
    }
    const expression = stmt.getExpression();
    if (!expression || expression.getKind() !== SyntaxKind.StringLiteral) {
      break;
    }
    if (expression.getLiteralText() === "use client") {
      return true;
    }
  }
  return false;
}

export function collectFacts(project, sourceFile) {
  const relPath = sourceFile.getFilePath();
  const checker = project.getTypeChecker();
  const { units, occurrences } = collectUnits(sourceFile, checker);
  return {
    filePath: relPath,
    sha256: createHash("sha256").update(readFileSync(sourceFile.getFilePath())).digest("hex"),
    units,
    imports: collectImportRecords(sourceFile),
    exports: collectExportRecords(sourceFile, checker),
    occurrences,
    extends: [],
    implements: [],
    hasUseClientDirective: hasUseClientDirective(sourceFile),
  };
}
