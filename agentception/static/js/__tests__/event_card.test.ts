import { describe, it, expect, beforeEach } from 'vitest';
import { attachEventCardHandler } from '../event_card';

function makeSource(): EventSource {
  return new EventTarget() as unknown as EventSource;
}

function dispatch(src: EventTarget, data: object): void {
  src.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(data) }));
}

describe('attachEventCardHandler', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="activity-feed"></div>';
  });

  it('renders step_start card with correct text', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'step_start', payload: { step: 'Step 2' }, recorded_at: '' });
    const card = document.querySelector('.event-card');
    expect(card).not.toBeNull();
    expect(card?.getAttribute('data-event-type')).toBe('step_start');
    expect(card?.querySelector('.event-card__text')?.textContent).toBe('Step 2');
  });

  it('renders done card with summary text', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'done', payload: { summary: 'All green' }, recorded_at: '' });
    expect(document.querySelector('.event-card__text')?.textContent).toBe('All green');
  });

  it('renders blocker card with correct data-event-type and icon', () => {
    const src = makeSource();
    attachEventCardHandler(src);
    dispatch(src, { t: 'event', event_type: 'blocker', payload: { description: 'Missing dep' }, recorded_at: '' });
    const card = document.querySelector('.event-card');
    expect(card?.getAttribute('data-event-type')).toBe('blocker');
    expect(card?.querySelector('.event-card__icon')?.textContent).toBe('🚧');
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
});
