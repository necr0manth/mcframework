export function optimisticActivation(snapshot, ids, activation, groupId = '') {
  const targets = new Set(ids);
  return {
    ...snapshot,
    packages: snapshot.packages.map((item) => targets.has(item.id)
      ? { ...item, activation }
      : item),
    groups: snapshot.groups.map((item) => item.id === groupId
      ? { ...item, activation }
      : item),
  };
}

export function applyPendingMutations(snapshot, pending) {
  return pending.reduce((current, operation) => operation.optimistic
    ? operation.optimistic(current)
    : current, snapshot);
}

export function applyCompactReply(snapshot, reply) {
  if (!snapshot || reply.revision < snapshot.revision) return snapshot;
  const packages = new Map((reply.activation_patch?.packages || []).map((item) => [item.id, item]));
  const groups = new Map((reply.activation_patch?.groups || []).map((item) => [item.id, item]));
  return {
    ...snapshot,
    revision: reply.revision,
    packages: snapshot.packages.map((item) => packages.has(item.id)
      ? { ...item, ...packages.get(item.id) } : item),
    groups: snapshot.groups.map((item) => groups.has(item.id)
      ? { ...item, ...groups.get(item.id) } : item),
    delta: { ...snapshot.delta, loaded: false, complete: false, revision: reply.revision },
  };
}

export function settleCompactReply(snapshot, reply, optimistic) {
  if (!snapshot || reply.revision < snapshot.revision) return snapshot;
  return applyCompactReply(optimistic ? optimistic(snapshot) : snapshot, reply);
}
