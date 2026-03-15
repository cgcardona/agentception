import { describe, it, expect } from 'vitest';
import { buildFileEditCard } from '../file_edit_card';

const base = { path: 'src/foo.py', diff: '', lines_omitted: 0, timestamp: '' };

describe('buildFileEditCard', () => {
  it('renders diff-add class for + lines', () => {
    const card = buildFileEditCard({ ...base, diff: '+foo\n' });
    const spans = card.querySelectorAll('.diff-add');
    expect(spans.length).toBe(1);
    expect(spans[0].textContent).toContain('+foo');
  });

  it('does not treat +++ header as a diff-add line', () => {
    const card = buildFileEditCard({ ...base, diff: '+++ b/file.py\n+added line\n' });
    expect(card.querySelectorAll('.diff-add').length).toBe(1);
  });

  it('renders diff-remove class for - lines', () => {
    const card = buildFileEditCard({ ...base, diff: '-bar\n' });
    expect(card.querySelectorAll('.diff-remove').length).toBe(1);
  });

  it('does not treat --- header as a diff-remove line', () => {
    const card = buildFileEditCard({ ...base, diff: '--- a/file.py\n-removed\n' });
    expect(card.querySelectorAll('.diff-remove').length).toBe(1);
  });

  it('renders diff-hunk class for @@ lines', () => {
    const card = buildFileEditCard({ ...base, diff: '@@ -1,3 +1,4 @@\n' });
    expect(card.querySelectorAll('.diff-hunk').length).toBe(1);
  });

  it('starts collapsed', () => {
    const card = buildFileEditCard(base);
    expect(card.classList.contains('collapsed')).toBe(true);
  });

  it('header is a <button> element', () => {
    const card = buildFileEditCard(base);
    const header = card.querySelector('.file-edit-card__header');
    expect(header?.tagName).toBe('BUTTON');
  });

  it('header shows basename not full path', () => {
    const card = buildFileEditCard({ ...base, path: 'agentception/services/agent_loop.py' });
    const pathEl = card.querySelector('.file-edit-card__path');
    expect(pathEl?.textContent).toBe('agent_loop.py');
  });

  it('header title attribute holds full path for hover', () => {
    const fullPath = 'agentception/services/agent_loop.py';
    const card = buildFileEditCard({ ...base, path: fullPath });
    const header = card.querySelector('.file-edit-card__header') as HTMLButtonElement | null;
    expect(header?.title).toBe(fullPath);
  });

  it('toggles collapsed on header click', () => {
    const card = buildFileEditCard(base);
    const header = card.querySelector('.file-edit-card__header') as HTMLElement;
    header.click();
    expect(card.classList.contains('collapsed')).toBe(false);
    header.click();
    expect(card.classList.contains('collapsed')).toBe(true);
  });

  it('shows line-count badge with +N for added lines', () => {
    const card = buildFileEditCard({ ...base, diff: '+added1\n+added2\n-removed\n' });
    const addBadge = card.querySelector('.file-edit-card__badge-add');
    const removeBadge = card.querySelector('.file-edit-card__badge-remove');
    expect(addBadge?.textContent).toBe('+2');
    expect(removeBadge?.textContent).toBe('-1');
  });

  it('shows no badge when diff is empty', () => {
    const card = buildFileEditCard({ ...base, diff: '' });
    expect(card.querySelector('.file-edit-card__badge')).toBeNull();
  });

  it('shows omitted lines message when lines_omitted > 0', () => {
    const card = buildFileEditCard({ ...base, lines_omitted: 5 });
    const msg = card.querySelector('.diff-omitted');
    expect(msg?.textContent).toContain('5 more lines');
  });

  it('diff is inside a <pre> that is a direct child of the card', () => {
    const card = buildFileEditCard({ ...base, diff: '+line\n' });
    const pre = card.querySelector(':scope > pre');
    expect(pre).not.toBeNull();
  });
});
