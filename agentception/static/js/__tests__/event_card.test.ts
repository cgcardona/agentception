import { describe, it, expect, beforeEach } from 'vitest';
import { attachEventCardHandler } from '../event_card';
import { resetStepContext } from '../step_context';

function makeSource(): EventSource {
  return new EventTarget() as unknown as EventSource;
}

function dispatch(src: EventTarget, data: object): void {
  src.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(data) }));
}

describe('attachEventCardHandler', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="activity-feed"></div>';
    resetStepContext();
  });

  it('renders step_start card inside a .step-group wrapper', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'step_start', payload: { step: 'Step 2' }, recorded_at: '' });
    const group = document.querySelector('.step-group');
    expect(group).not.toBeNull();
    const card = group?.querySelector('.event-card');
    expect(card).not.toBeNull();
    expect(card?.getAttribute('data-event-type')).toBe('step_start');
    expect(card?.querySelector('.event-card__text')?.textContent).toBe('Step 2');
  });

  it('renders step_start icon as SVG', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'step_start', payload: { step: 'Step 1' }, recorded_at: '' });
    const icon = document.querySelector('.event-card__icon');
    expect(icon?.innerHTML).toContain('<svg');
  });

  it('collapses the previous step group when a new step_start arrives', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'step_start', payload: { step: 'Step 1' }, recorded_at: '' });
    dispatch(src, { t: 'event', event_type: 'step_start', payload: { step: 'Step 2' }, recorded_at: '' });
    const groups = document.querySelectorAll('.step-group');
    expect(groups.length).toBe(2);
    expect(groups[0]?.classList.contains('step-group--collapsed')).toBe(true);
    expect(groups[1]?.classList.contains('step-group--current')).toBe(true);
  });

  it('step_start card is clickable (has role=button)', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'step_start', payload: { step: 'Step 1' }, recorded_at: '' });
    const card = document.querySelector('.event-card[data-event-type="step_start"]');
    expect(card?.getAttribute('role')).toBe('button');
    expect(card?.getAttribute('aria-expanded')).toBe('true');
  });

  it('renders done card with summary text', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'done', payload: { summary: 'All green' }, recorded_at: '' });
    expect(document.querySelector('.event-card__text')?.textContent).toBe('All green');
  });

  it('renders done icon as SVG', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'done', payload: { summary: 'ok' }, recorded_at: '' });
    expect(document.querySelector('.event-card__icon')?.innerHTML).toContain('<svg');
  });

  it('renders blocker card with correct data-event-type and SVG icon', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'blocker', payload: { description: 'Missing dep' }, recorded_at: '' });
    const card = document.querySelector('.event-card');
    expect(card?.getAttribute('data-event-type')).toBe('blocker');
    expect(card?.querySelector('.event-card__icon')?.innerHTML).toContain('<svg');
  });

  it('ignores non-event SSE messages', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'thought', role: 'thinking', content: 'hmm', recorded_at: '' });
    expect(document.querySelector('.event-card')).toBeNull();
  });

  it('ignores unknown event_type', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'file_edit', payload: {}, recorded_at: '' });
    expect(document.querySelector('.event-card')).toBeNull();
  });

  it('renders message card with SVG icon and message text', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'message', payload: { message: 'Branch created' }, recorded_at: '' });
    const card = document.querySelector('.event-card');
    expect(card).not.toBeNull();
    expect(card?.getAttribute('data-event-type')).toBe('message');
    expect(card?.querySelector('.event-card__icon')?.innerHTML).toContain('<svg');
    expect(card?.querySelector('.event-card__text')?.textContent).toBe('Branch created');
  });

  it('renders error card with SVG icon and error text', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'error', payload: { error: 'Rate limit hit' }, recorded_at: '' });
    const card = document.querySelector('.event-card');
    expect(card).not.toBeNull();
    expect(card?.getAttribute('data-event-type')).toBe('error');
    expect(card?.querySelector('.event-card__icon')?.innerHTML).toContain('<svg');
    expect(card?.querySelector('.event-card__text')?.textContent).toBe('Rate limit hit');
  });

  it('renders decision card with SVG icon and composed text', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, {
      t: 'event',
      event_type: 'decision',
      payload: { decision: 'Use TypeScript', rationale: 'type safety' },
      recorded_at: '',
    });
    const card = document.querySelector('.event-card');
    expect(card?.querySelector('.event-card__icon')?.innerHTML).toContain('<svg');
    expect(card?.querySelector('.event-card__text')?.textContent).toBe('Use TypeScript — type safety');
  });
});
