'use client'

import React from 'react'

interface ErrorBoundaryProps {
  children: React.ReactNode
  fallback?: React.ReactNode | ((error: Error, reset: () => void) => React.ReactNode)
  onError?: (error: Error, info: React.ErrorInfo) => void
}

interface ErrorBoundaryState {
  hasError: boolean
  error: Error | null
}

export class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    this.props.onError?.(error, info)
    console.error('[ErrorBoundary]', error, info)
  }

  reset = () => {
    this.setState({ hasError: false, error: null })
  }

  render() {
    if (this.state.hasError && this.state.error) {
      if (typeof this.props.fallback === 'function') {
        return this.props.fallback(this.state.error, this.reset)
      }
      if (this.props.fallback) {
        return this.props.fallback
      }
      return (
        <DefaultErrorFallback error={this.state.error} reset={this.reset} />
      )
    }
    return this.props.children
  }
}

function DefaultErrorFallback({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div className="error-boundary" role="alert">
      <div className="error-boundary__icon" aria-hidden="true">⚠️</div>
      <h3 className="error-boundary__title">오류가 발생했습니다</h3>
      <p className="error-boundary__message">{error.message}</p>
      <button className="btn btn--primary btn--sm" onClick={reset} type="button">
        다시 시도
      </button>

      <style>{`
        .error-boundary {
          display: flex; flex-direction: column; align-items: center;
          gap: 12px; padding: var(--space-2xl, 48px) var(--space-lg, 24px);
          text-align: center;
          background: var(--color-surface, #fef2f2);
          border: 1px solid var(--color-danger, #fca5a5);
          border-radius: var(--radius-lg, 12px);
        }
        .error-boundary__icon { font-size: 2.5rem; }
        .error-boundary__title { font-size: 1.0625rem; font-weight: 600; color: var(--color-text, #1a1a1a); margin: 0; }
        .error-boundary__message { font-size: 0.875rem; color: var(--color-text-muted, #64748b); max-width: 360px; margin: 0; }
      `}</style>
    </div>
  )
}
