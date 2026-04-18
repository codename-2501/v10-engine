'use client'

import React from 'react'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md' | 'lg'

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  leftIcon?: React.ReactNode
  rightIcon?: React.ReactNode
  fullWidth?: boolean
}

export const Button = React.memo(function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  leftIcon,
  rightIcon,
  fullWidth = false,
  children,
  disabled,
  className = '',
  ...props
}: ButtonProps) {
  const isDisabled = disabled || loading

  return (
    <button
      className={`btn btn--${variant} btn--${size}${fullWidth ? ' btn--full' : ''}${isDisabled ? ' btn--disabled' : ''} ${className}`.trim()}
      disabled={isDisabled}
      aria-disabled={isDisabled}
      {...props}
    >
      {loading && (
        <span className="btn__spinner" aria-hidden="true">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="2" strokeDasharray="25 13" />
          </svg>
        </span>
      )}
      {!loading && leftIcon && <span className="btn__icon btn__icon--left" aria-hidden="true">{leftIcon}</span>}
      <span className="btn__label">{children}</span>
      {!loading && rightIcon && <span className="btn__icon btn__icon--right" aria-hidden="true">{rightIcon}</span>}

      <style>{`
        .btn {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 6px;
          font-family: var(--font-body, var(--font-family, sans-serif));
          font-weight: 500;
          border: 1px solid transparent;
          border-radius: var(--radius-md, 8px);
          cursor: pointer;
          transition: background 0.15s, color 0.15s, border-color 0.15s, box-shadow 0.15s, opacity 0.15s;
          white-space: nowrap;
          text-decoration: none;
          outline: none;
          position: relative;
          user-select: none;
        }
        .btn:focus-visible {
          box-shadow: 0 0 0 3px var(--color-primary, #3b82f6), 0 0 0 5px var(--color-bg, #fff);
        }
        .btn--sm { padding: 6px 12px; font-size: 0.8125rem; }
        .btn--md { padding: 9px 18px; font-size: 0.9375rem; }
        .btn--lg { padding: 12px 24px; font-size: 1.0625rem; }
        .btn--full { width: 100%; }
        .btn--primary {
          background: var(--color-primary, #3b82f6);
          color: #fff;
        }
        .btn--primary:hover:not(.btn--disabled) {
          background: var(--color-primary-hover, #2563eb);
        }
        .btn--secondary {
          background: var(--color-surface, #f1f5f9);
          color: var(--color-text, #1a1a1a);
          border-color: var(--color-border, #e2e8f0);
        }
        .btn--secondary:hover:not(.btn--disabled) {
          background: var(--color-border, #e2e8f0);
        }
        .btn--ghost {
          background: transparent;
          color: var(--color-primary, #3b82f6);
        }
        .btn--ghost:hover:not(.btn--disabled) {
          background: var(--color-surface, #f1f5f9);
        }
        .btn--danger {
          background: var(--color-danger, #ef4444);
          color: #fff;
        }
        .btn--danger:hover:not(.btn--disabled) {
          background: var(--color-danger-hover, #dc2626);
        }
        .btn--disabled {
          opacity: 0.5;
          cursor: not-allowed;
          pointer-events: none;
        }
        .btn__spinner {
          display: inline-flex;
          animation: btn-spin 0.8s linear infinite;
        }
        @keyframes btn-spin { to { transform: rotate(360deg); } }
        .btn__icon { display: inline-flex; align-items: center; }
        @media (prefers-reduced-motion: reduce) {
          .btn { transition: none; }
          .btn__spinner { animation: none; }
        }
      `}</style>
    </button>
  )
})
