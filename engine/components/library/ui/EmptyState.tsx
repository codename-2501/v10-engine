'use client'

import React from 'react'

export interface EmptyStateProps {
  icon?: React.ReactNode
  title: string
  description?: string
  action?: React.ReactNode
  className?: string
  compact?: boolean
}

export const EmptyState = React.memo(function EmptyState({
  icon,
  title,
  description,
  action,
  className = '',
  compact = false,
}: EmptyStateProps) {
  return (
    <div className={`empty-state${compact ? ' empty-state--compact' : ''} ${className}`.trim()} role="region" aria-label={title}>
      {icon && <div className="empty-state__icon" aria-hidden="true">{icon}</div>}
      <h3 className="empty-state__title">{title}</h3>
      {description && <p className="empty-state__desc">{description}</p>}
      {action && <div className="empty-state__action">{action}</div>}

      <style>{`
        .empty-state {
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          text-align: center; gap: 12px;
          padding: var(--space-2xl, 48px) var(--space-lg, 24px);
          color: var(--color-text-muted, #64748b);
        }
        .empty-state--compact { padding: var(--space-lg, 24px) var(--space-md, 16px); gap: 8px; }
        .empty-state__icon {
          font-size: 3rem; line-height: 1;
          color: var(--color-border, #cbd5e1);
        }
        .empty-state--compact .empty-state__icon { font-size: 2rem; }
        .empty-state__title {
          font-size: 1.0625rem; font-weight: 600;
          color: var(--color-text, #1a1a1a); margin: 0;
        }
        .empty-state--compact .empty-state__title { font-size: 0.9375rem; }
        .empty-state__desc { font-size: 0.9375rem; max-width: 400px; margin: 0; line-height: 1.6; }
        .empty-state__action { margin-top: 4px; }
      `}</style>
    </div>
  )
})
