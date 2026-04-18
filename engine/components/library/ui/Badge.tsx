'use client'

import React from 'react'

export type BadgeVariant = 'default' | 'primary' | 'success' | 'warning' | 'danger' | 'info' | 'outline'
export type BadgeSize = 'sm' | 'md' | 'lg'

export interface BadgeProps {
  variant?: BadgeVariant
  size?: BadgeSize
  dot?: boolean
  children: React.ReactNode
  className?: string
}

export const Badge = React.memo(function Badge({
  variant = 'default',
  size = 'md',
  dot = false,
  children,
  className = '',
}: BadgeProps) {
  return (
    <span className={`badge badge--${variant} badge--${size} ${className}`.trim()}>
      {dot && <span className="badge__dot" aria-hidden="true" />}
      {children}

      <style>{`
        .badge {
          display: inline-flex; align-items: center; gap: 5px;
          font-weight: 500; white-space: nowrap;
          border-radius: 999px;
        }
        .badge--sm { padding: 2px 8px; font-size: 0.7rem; }
        .badge--md { padding: 3px 10px; font-size: 0.75rem; }
        .badge--lg { padding: 4px 12px; font-size: 0.8125rem; }
        .badge--default { background: var(--color-surface, #f1f5f9); color: var(--color-text-muted, #64748b); }
        .badge--primary { background: color-mix(in srgb, var(--color-primary, #3b82f6) 15%, transparent); color: var(--color-primary, #3b82f6); }
        .badge--success { background: color-mix(in srgb, var(--color-success, #22c55e) 15%, transparent); color: var(--color-success, #22c55e); }
        .badge--warning { background: color-mix(in srgb, var(--color-warning, #f59e0b) 15%, transparent); color: var(--color-warning-text, #92400e); }
        .badge--danger { background: color-mix(in srgb, var(--color-danger, #ef4444) 15%, transparent); color: var(--color-danger, #ef4444); }
        .badge--info { background: color-mix(in srgb, var(--color-info, #06b6d4) 15%, transparent); color: var(--color-info, #06b6d4); }
        .badge--outline { background: transparent; border: 1px solid var(--color-border, #e2e8f0); color: var(--color-text-muted, #64748b); }
        .badge__dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
      `}</style>
    </span>
  )
})
