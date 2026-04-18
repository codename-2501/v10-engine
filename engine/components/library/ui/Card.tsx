'use client'

import React from 'react'

export interface CardProps {
  title?: string
  description?: string
  footer?: React.ReactNode
  headerAction?: React.ReactNode
  padding?: 'none' | 'sm' | 'md' | 'lg'
  bordered?: boolean
  elevated?: boolean
  className?: string
  children?: React.ReactNode
  onClick?: () => void
  as?: React.ElementType
}

export const Card = React.memo(function Card({
  title,
  description,
  footer,
  headerAction,
  padding = 'md',
  bordered = true,
  elevated = false,
  className = '',
  children,
  onClick,
  as: Tag = 'div',
}: CardProps) {
  const isInteractive = !!onClick

  return (
    <Tag
      className={`card card--pad-${padding}${bordered ? ' card--bordered' : ''}${elevated ? ' card--elevated' : ''}${isInteractive ? ' card--interactive' : ''} ${className}`.trim()}
      onClick={onClick}
      tabIndex={isInteractive ? 0 : undefined}
      role={isInteractive ? 'button' : undefined}
      onKeyDown={isInteractive ? (e: React.KeyboardEvent) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } } : undefined}
    >
      {(title || headerAction) && (
        <div className="card__header">
          <div className="card__header-left">
            {title && <h3 className="card__title">{title}</h3>}
            {description && <p className="card__description">{description}</p>}
          </div>
          {headerAction && <div className="card__header-action">{headerAction}</div>}
        </div>
      )}
      {children && <div className="card__body">{children}</div>}
      {footer && <div className="card__footer">{footer}</div>}

      <style>{`
        .card {
          background: var(--color-surface, #fff);
          border-radius: var(--radius-lg, 12px);
          overflow: hidden;
          display: flex; flex-direction: column;
        }
        .card--bordered { border: 1px solid var(--color-border, #e2e8f0); }
        .card--elevated { box-shadow: var(--shadow-md, 0 4px 16px rgba(0,0,0,0.08)); }
        .card--interactive { cursor: pointer; transition: box-shadow 0.15s, transform 0.15s; }
        .card--interactive:hover { box-shadow: var(--shadow-lg, 0 8px 24px rgba(0,0,0,0.12)); transform: translateY(-2px); }
        .card--interactive:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); outline-offset: 2px; }
        .card--pad-none .card__header, .card--pad-none .card__body { padding: 0; }
        .card--pad-sm .card__header, .card--pad-sm .card__body { padding: var(--space-sm, 8px) var(--space-md, 16px); }
        .card--pad-md .card__header, .card--pad-md .card__body { padding: var(--space-md, 16px) var(--space-lg, 24px); }
        .card--pad-lg .card__header, .card--pad-lg .card__body { padding: var(--space-lg, 24px) var(--space-xl, 32px); }
        .card__header {
          display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
          padding-bottom: 0;
        }
        .card__header-left { flex: 1; }
        .card__header-action { flex-shrink: 0; }
        .card__title { font-size: 1rem; font-weight: 600; color: var(--color-text, #1a1a1a); margin: 0; }
        .card__description { font-size: 0.875rem; color: var(--color-text-muted, #64748b); margin: 4px 0 0; }
        .card__body { flex: 1; }
        .card__footer {
          border-top: 1px solid var(--color-border, #e2e8f0);
          padding: var(--space-md, 16px) var(--space-lg, 24px);
          background: var(--color-bg, #f8fafc);
        }
        @media (prefers-reduced-motion: reduce) { .card--interactive { transition: none; } }
      `}</style>
    </Tag>
  )
})


export interface StatCardProps {
  label: string
  value: string | number
  delta?: string
  deltaType?: 'up' | 'down' | 'neutral'
  icon?: React.ReactNode
  className?: string
}

export const StatCard = React.memo(function StatCard({
  label,
  value,
  delta,
  deltaType = 'neutral',
  icon,
  className = '',
}: StatCardProps) {
  return (
    <div className={`stat-card ${className}`.trim()}>
      <div className="stat-card__top">
        <span className="stat-card__label">{label}</span>
        {icon && <span className="stat-card__icon" aria-hidden="true">{icon}</span>}
      </div>
      <div className="stat-card__value" aria-label={`${label}: ${value}`}>{value}</div>
      {delta && (
        <div className={`stat-card__delta stat-card__delta--${deltaType}`}>
          <span aria-hidden="true">{deltaType === 'up' ? '↑' : deltaType === 'down' ? '↓' : '→'}</span>
          {delta}
        </div>
      )}

      <style>{`
        .stat-card {
          background: var(--color-surface, #fff);
          border: 1px solid var(--color-border, #e2e8f0);
          border-radius: var(--radius-lg, 12px);
          padding: var(--space-lg, 24px);
          display: flex; flex-direction: column; gap: 8px;
        }
        .stat-card__top { display: flex; align-items: center; justify-content: space-between; }
        .stat-card__label { font-size: 0.875rem; color: var(--color-text-muted, #64748b); font-weight: 500; }
        .stat-card__icon { color: var(--color-primary, #3b82f6); font-size: 1.25rem; }
        .stat-card__value { font-size: 2rem; font-weight: 700; color: var(--color-text, #1a1a1a); line-height: 1.1; }
        .stat-card__delta { font-size: 0.8125rem; font-weight: 500; display: flex; align-items: center; gap: 4px; }
        .stat-card__delta--up { color: var(--color-success, #22c55e); }
        .stat-card__delta--down { color: var(--color-danger, #ef4444); }
        .stat-card__delta--neutral { color: var(--color-text-muted, #64748b); }
      `}</style>
    </div>
  )
})
