import { CheckCheck, LoaderCircle, ShieldCheck } from 'lucide-react';
import { Badge } from './components';
import type { GoalReview } from './types';
import './goalReview.css';

const labels: Record<GoalReview['status'], string> = {
  reviewing: 'Reviewing completion',
  complete: 'Completion verified',
  needs_changes: 'Changes needed',
  insufficient_evidence: 'More evidence needed',
  error: 'Review unavailable',
  disabled: 'Independent review off',
};

export function GoalReviewCard({ review, paused = false }: { review?: GoalReview; paused?: boolean }) {
  if (!review) return null;
  return <section className={`goal-review-card ${review.status}`} aria-label="Independent goal review">
    <header>{review.status === 'reviewing' && !paused ? <LoaderCircle size={15} className="spin" /> : review.status === 'complete' ? <CheckCheck size={15} /> : <ShieldCheck size={15} />}<strong>{review.status === 'reviewing' && paused ? 'Review paused' : labels[review.status] || 'Goal review'}</strong>{review.attempt ? <Badge>Review {review.attempt}</Badge> : null}</header>
    <small>OpenRouter · Independent, read-only review</small>
    {review.summary && <p>{review.summary}</p>}
    {review.feedback?.length ? <details open={review.status !== 'complete'}><summary>Reviewer feedback · {review.feedback.length}</summary><ol>{review.feedback.map((item, index) => <li key={index}>{item}</li>)}</ol></details> : null}
    {review.status === 'reviewing' && !paused && <p className="muted">Checking the goal, checklist and recorded evidence before completion.</p>}
    {review.status === 'needs_changes' && !paused && <p className="muted">The main agent receives these fixes and continues the goal.</p>}
    {paused && review.status !== 'complete' && review.status !== 'disabled' && <p className="review-recovery">Goal paused without verified completion. Resolve the review issue, then Resume.</p>}
  </section>;
}
