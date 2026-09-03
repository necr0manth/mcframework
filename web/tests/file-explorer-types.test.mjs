import assert from 'node:assert/strict';
import { fileURLToPath, URL } from 'node:url';
import test from 'node:test';
import ts from 'typescript';

test('capture decisions preserve their strict TypeScript contract', () => {
  const filename = fileURLToPath(new URL('./capture-contract.ts', import.meta.url)).replaceAll('\\', '/');
  const source = `
    import { decisionsForPlan } from '../src/file-explorer-model.js';
    import type { CaptureDecision } from '../src/file-explorer-model.js';

    const automatic = decisionsForPlan([
      { id: 'text', path: 'a.txt', capture_modes: ['payload'], expected_sha256: 'sha' },
      { id: 'removed', path: 'b.txt', capture_modes: ['remove'], expected_absent: true },
    ], {});
    const explicit = decisionsForPlan([
      { id: 'jar', path: 'a.jar', capture_modes: ['package', 'payload'], expected_sha256: 'sha' },
    ], { jar: { mode: 'package', provider_match: { confidence: 'fuzzy' }, fuzzyConfirmed: true } });
    const capturePath = (_decisions: Record<string, unknown>) => {};
    capturePath(automatic);
    capturePath(explicit);
    const decision: CaptureDecision = explicit.jar;
    const mode: string = decision.mode;

    type Assert<T extends true> = T;
    export type InvalidExpectations = Assert<
      { mode: string } extends CaptureDecision ? false : true
    >;
    export type ConflictingExpectations = Assert<
      { mode: string; expected_sha256: string; expected_absent: true } extends CaptureDecision
        ? false : true
    >;
  `;
  const options = {
    strict: true, allowJs: true, noEmit: true, skipLibCheck: true,
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
  };
  const host = ts.createCompilerHost(options);
  const getSourceFile = host.getSourceFile.bind(host);
  host.getSourceFile = (path, languageVersion, onError) => path === filename
    ? ts.createSourceFile(path, source, languageVersion, true)
    : getSourceFile(path, languageVersion, onError);
  const program = ts.createProgram([filename], options, host);
  const diagnostics = ts.getPreEmitDiagnostics(program);
  assert.deepEqual(diagnostics.map((entry) =>
    ts.flattenDiagnosticMessageText(entry.messageText, '\n')), []);
});
