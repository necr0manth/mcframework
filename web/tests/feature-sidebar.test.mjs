import assert from 'node:assert/strict';
import test from 'node:test';

import { normalizeBootstrap } from '../src/api.js';
import { featureSidebarSummary } from '../src/view-model.js';

test('shows the effective layer and branch with active features', () => {
  const { link } = normalizeBootstrap({
    link: { branch: 'optional', effective_layer: '.mcpack/features/demo', features: ['extra'] },
  });

  assert.equal(featureSidebarSummary(link),
    'The instance resolves from .mcpack/features/demo on branch optional.');
});

test('removing the last feature still reports the retained overlay', () => {
  const { link } = normalizeBootstrap({
    revision: 12,
    link: { branch: 'optional', effective_layer: '.mcpack/features/demo', features: [] },
  });

  assert.equal(featureSidebarSummary(link),
    'No features are active. The instance resolves from .mcpack/features/demo on branch optional.');
});

test('a local edits overlay without features reports its actual layer', () => {
  const { link } = normalizeBootstrap({
    link: { branch: 'client', effective_layer: '.mcpack/local/client-edits', features: [] },
  });

  assert.equal(featureSidebarSummary(link),
    'No features are active. The instance resolves from .mcpack/local/client-edits on branch client.');
});

test('an instance without an overlay reports the branch as the effective layer', () => {
  const { link } = normalizeBootstrap({
    link: { branch: 'optional', effective_layer: 'optional', features: [] },
  });

  assert.equal(featureSidebarSummary(link),
    'No features are active. The instance resolves from optional on branch optional.');
});
