/** Presentation groups only: keep every supplied destination in its original order. */
const toolGroups = new Map([
  ['builder', 'Work'], ['spaces', 'Work'], ['scheduled', 'Work'],
  ['plugins', 'Manage'], ['agents', 'Manage'], ['usage', 'Inspect'], ['memory', 'Inspect'],
]);

export function groupWorkspaceTools<T extends {page: string}>(items: readonly T[]) {
  const groups: {label: string; key: string; items: T[]; itemKeys: string[]}[] = [];
  const groupCounts = new Map<string, number>(), destinationCounts = new Map<string, number>();
  for (const item of items) {
    if (item.page === 'projects') continue;
    const label = toolGroups.get(item.page) || 'Other tools';
    const occurrence = destinationCounts.get(item.page) || 0;
    destinationCounts.set(item.page, occurrence + 1);
    const itemKey = `${item.page}:${occurrence}`;
    const previous = groups[groups.length - 1];
    if (previous?.label === label) {previous.items.push(item);previous.itemKeys.push(itemKey);}
    else {
      const groupOccurrence = groupCounts.get(label) || 0;
      groupCounts.set(label, groupOccurrence + 1);
      groups.push({label, key: `${label}:${groupOccurrence}`, items: [item], itemKeys: [itemKey]});
    }
  }
  return groups;
}
