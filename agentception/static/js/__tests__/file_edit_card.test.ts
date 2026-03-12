import { describe, it, expect } from 'vitest';
import { buildFileEditCard } from '../file_edit_card';

const base = { path: 'foo.py', diff: '', lines_omitted: 0, timestamp: '' };

describe('buildFileEditCard', () => {
  it('renders diff-add class for + lines', () => {
    const card = buildFileEditCard({ ...base, diff: '+foo\n' });
    const spans = card.querySelectorAll('.diff-add');
    expect(spans.length).toBe(1);
    expect(spans[0].textContent).toContain('+foo');
  });

  it('renders diff-remove class for - lines', () => {
    const card = buildFileEditCard({ ...base, diff: '-bar\n' });
    expect(card.querySelectorAll('.diff-remove').length).toBe(1);
  });

  it('starts collapsed', () => {
    const card = buildFileEditCard(base);
    expect(card.classList.contains('collapsed')).toBe(true);
  });

  it('toggles collapsed on header click', () => {
    const card = buildFileEditCard(base);
    const header = card.querySelector('.file-edit-card__header') as HTMLElement;
    header.click();
    expect(card.classList.contains('collapsed')).toBe(false);
  });

  it('shows omitted lines message when lines_omitted > 0', () => {
    const card = buildFileEditCard({ ...base, lines_omitted: 5 });
    const msg = card.querySelector('.diff-omitted');
    expect(msg?.textContent).toContain('5 more lines');
  });
});
