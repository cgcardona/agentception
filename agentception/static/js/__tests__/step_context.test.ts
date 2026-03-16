import { describe, it, expect, beforeEach } from 'vitest';
import { getCurrentAppendTarget, getCurrentStepHeader, openStepGroup, resetStepContext } from '../step_context';

function makeFeed(): HTMLElement {
  const el = document.createElement('div');
  el.id = 'activity-feed';
  document.body.appendChild(el);
  return el;
}

function makeHeader(): HTMLElement {
  const el = document.createElement('div');
  el.className = 'event-card';
  el.dataset['eventType'] = 'step_start';
  return el;
}

describe('step_context', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    resetStepContext();
  });

  it('getCurrentAppendTarget returns feed when no step is open', () => {
    const feed = makeFeed();
    expect(getCurrentAppendTarget(feed)).toBe(feed);
  });

  it('getCurrentAppendTarget returns step body after openStepGroup', () => {
    const feed = makeFeed();
    openStepGroup(feed, makeHeader());
    const target = getCurrentAppendTarget(feed);
    expect(target).not.toBe(feed);
    expect(target.classList.contains('step-group__body')).toBe(true);
  });

  it('openStepGroup appends a .step-group to feed', () => {
    const feed = makeFeed();
    openStepGroup(feed, makeHeader());
    expect(feed.querySelector('.step-group')).not.toBeNull();
  });

  it('openStepGroup marks the new group --current', () => {
    const feed = makeFeed();
    openStepGroup(feed, makeHeader());
    expect(feed.querySelector('.step-group--current')).not.toBeNull();
  });

  it('openStepGroup collapses the previous group on the second call', () => {
    const feed = makeFeed();
    openStepGroup(feed, makeHeader());
    openStepGroup(feed, makeHeader());
    const groups = feed.querySelectorAll('.step-group');
    expect(groups.length).toBe(2);
    expect(groups[0]?.classList.contains('step-group--collapsed')).toBe(true);
    expect(groups[1]?.classList.contains('step-group--current')).toBe(true);
  });

  it('resetStepContext makes getCurrentAppendTarget return feed again', () => {
    const feed = makeFeed();
    openStepGroup(feed, makeHeader());
    resetStepContext();
    expect(getCurrentAppendTarget(feed)).toBe(feed);
  });

  it('the header element is placed inside the step-group', () => {
    const feed = makeFeed();
    const header = makeHeader();
    openStepGroup(feed, header);
    const group = feed.querySelector('.step-group');
    expect(group?.contains(header)).toBe(true);
  });

  it('getCurrentStepHeader returns null before any step is opened', () => {
    expect(getCurrentStepHeader()).toBeNull();
  });

  it('getCurrentStepHeader returns the header element after openStepGroup', () => {
    const feed = makeFeed();
    const header = makeHeader();
    openStepGroup(feed, header);
    expect(getCurrentStepHeader()).toBe(header);
  });

  it('getCurrentStepHeader updates to the latest header on second openStepGroup', () => {
    const feed = makeFeed();
    const header1 = makeHeader();
    const header2 = makeHeader();
    openStepGroup(feed, header1);
    openStepGroup(feed, header2);
    expect(getCurrentStepHeader()).toBe(header2);
  });

  it('resetStepContext clears getCurrentStepHeader', () => {
    const feed = makeFeed();
    openStepGroup(feed, makeHeader());
    resetStepContext();
    expect(getCurrentStepHeader()).toBeNull();
  });
});
