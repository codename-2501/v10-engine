'use client'

import React from 'react'

export interface SpinnerProps {
  size?: 'sm' | 'md' | 'lg'
  label?: string
  className?: string
}

export const Spinner = React.memo(function Spinner({ size = 'md', label = '로딩 중...', className = '' }: SpinnerProps) {
  const sizeMap = { sm: 20, md: 32, lg: 48 }
  const px = sizeMap[size]

  return (
    <span
      className={`spinner spinner--${size} ${className}`.trim()}
      role="status"
      aria-label={label}
    >
      <svg width={px} height={px} viewBox="0 0 32 32" fill="none" aria-hidden="true">
        <circle cx="16" cy="16" r="13" stroke="var(--color-border, #e2e8f0)" strokeWidth="3"/>
        <circle
          cx="16" cy="16" r="13"
          stroke="var(--color-primary, #3b82f6)"
          strokeWidth="3"
          strokeDasharray="30 52"
          strokeLinecap="round"
        />
      </svg>

      <style>{`
        .spinner { display: inline-flex; align-items: center; justify-content: center; }
        .spinner svg { animation: spinner-rotate 0.8s linear infinite; }
        @keyframes spinner-rotate { to { transform: rotate(360deg); } }
        @media (prefers-reduced-motion: reduce) { .spinner svg { animation: none; } }
      `}</style>
    </span>
  )
})

export interface SkeletonProps {
  width?: string
  height?: string
  rounded?: boolean
  className?: string
  lines?: number
}

export const Skeleton = React.memo(function Skeleton({
  width = '100%',
  height = '16px',
  rounded = false,
  className = '',
  lines,
}: SkeletonProps) {
  if (lines && lines > 1) {
    return (
      <div className={`skeleton-group ${className}`.trim()} aria-hidden="true">
        {Array.from({ length: lines }).map((_, i) => (
          <span
            key={i}
            className="skeleton-line"
            style={{
              width: i === lines - 1 ? '70%' : '100%',
              height,
              borderRadius: rounded ? '999px' : '4px',
            }}
          />
        ))}
        <style>{`
          .skeleton-group { display: flex; flex-direction: column; gap: 8px; }
          .skeleton-line { display: block; background: linear-gradient(90deg, var(--color-surface, #f1f5f9) 25%, var(--color-border, #e2e8f0) 50%, var(--color-surface, #f1f5f9) 75%); background-size: 200% 100%; animation: shimmer 1.4s ease infinite; }
          @keyframes shimmer { to { background-position: -200% 0; } }
          @media (prefers-reduced-motion: reduce) { .skeleton-line { animation: none; } }
        `}</style>
      </div>
    )
  }

  return (
    <span
      className={`skeleton-line ${className}`.trim()}
      style={{ display: 'block', width, height, borderRadius: rounded ? '999px' : '4px' }}
      aria-hidden="true"
    >
      <style>{`
        .skeleton-line {
          background: linear-gradient(90deg, var(--color-surface, #f1f5f9) 25%, var(--color-border, #e2e8f0) 50%, var(--color-surface, #f1f5f9) 75%);
          background-size: 200% 100%;
          animation: shimmer 1.4s ease infinite;
        }
        @keyframes shimmer { to { background-position: -200% 0; } }
        @media (prefers-reduced-motion: reduce) { .skeleton-line { animation: none; } }
      `}</style>
    </span>
  )
})

export interface LoadingOverlayProps {
  visible: boolean
  label?: string
  fullScreen?: boolean
}

export const LoadingOverlay = React.memo(function LoadingOverlay({
  visible,
  label = '로딩 중...',
  fullScreen = false,
}: LoadingOverlayProps) {
  if (!visible) return null

  return (
    <div
      className={`loading-overlay${fullScreen ? ' loading-overlay--fullscreen' : ''}`}
      role="status"
      aria-live="polite"
      aria-label={label}
    >
      <Spinner size="lg" label={label} />
      <span className="loading-overlay__label">{label}</span>

      <style>{`
        .loading-overlay {
          position: absolute; inset: 0;
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          gap: 16px;
          background: color-mix(in srgb, var(--color-bg, #fff) 85%, transparent);
          backdrop-filter: blur(2px);
          z-index: 10;
          border-radius: inherit;
        }
        .loading-overlay--fullscreen { position: fixed; }
        .loading-overlay__label { font-size: 0.9375rem; color: var(--color-text-muted, #64748b); font-weight: 500; }
      `}</style>
    </div>
  )
})
