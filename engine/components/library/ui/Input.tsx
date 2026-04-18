'use client'

import React from 'react'

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string
  error?: string
  hint?: string
  leftAddon?: React.ReactNode
  rightAddon?: React.ReactNode
}

export const Input = React.memo(function Input({
  label,
  error,
  hint,
  leftAddon,
  rightAddon,
  id,
  className = '',
  required,
  ...props
}: InputProps) {
  const inputId = id || `input-${Math.random().toString(36).slice(2, 9)}`
  const errorId = error ? `${inputId}-error` : undefined
  const hintId = hint ? `${inputId}-hint` : undefined

  return (
    <div className={`field ${error ? 'field--error' : ''} ${className}`.trim()}>
      {label && (
        <label htmlFor={inputId} className="field__label">
          {label}
          {required && <span className="field__required" aria-hidden="true"> *</span>}
        </label>
      )}
      <div className="field__wrap">
        {leftAddon && <span className="field__addon field__addon--left">{leftAddon}</span>}
        <input
          id={inputId}
          className={`field__input${leftAddon ? ' field__input--pl' : ''}${rightAddon ? ' field__input--pr' : ''}`}
          aria-invalid={!!error}
          aria-describedby={[errorId, hintId].filter(Boolean).join(' ') || undefined}
          required={required}
          {...props}
        />
        {rightAddon && <span className="field__addon field__addon--right">{rightAddon}</span>}
      </div>
      {hint && !error && <span id={hintId} className="field__hint">{hint}</span>}
      {error && <span id={errorId} className="field__error" role="alert">{error}</span>}

      <style>{`
        .field { display: flex; flex-direction: column; gap: 6px; }
        .field__label {
          font-size: 0.875rem;
          font-weight: 500;
          color: var(--color-text, #1a1a1a);
        }
        .field__required { color: var(--color-danger, #ef4444); }
        .field__wrap { position: relative; display: flex; align-items: center; }
        .field__input {
          width: 100%;
          padding: 9px 14px;
          font-size: 0.9375rem;
          font-family: inherit;
          color: var(--color-text, #1a1a1a);
          background: var(--color-surface, #f8fafc);
          border: 1px solid var(--color-border, #e2e8f0);
          border-radius: var(--radius-md, 8px);
          outline: none;
          transition: border-color 0.15s, box-shadow 0.15s;
        }
        .field__input--pl { padding-left: 40px; }
        .field__input--pr { padding-right: 40px; }
        .field__input:focus {
          border-color: var(--color-primary, #3b82f6);
          box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-primary, #3b82f6) 20%, transparent);
        }
        .field--error .field__input {
          border-color: var(--color-danger, #ef4444);
        }
        .field--error .field__input:focus {
          box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-danger, #ef4444) 20%, transparent);
        }
        .field__addon {
          position: absolute;
          display: flex;
          align-items: center;
          color: var(--color-text-muted, #64748b);
        }
        .field__addon--left { left: 12px; }
        .field__addon--right { right: 12px; }
        .field__hint { font-size: 0.8125rem; color: var(--color-text-muted, #64748b); }
        .field__error { font-size: 0.8125rem; color: var(--color-danger, #ef4444); }
      `}</style>
    </div>
  )
})

export interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: string
  error?: string
  hint?: string
}

export const Textarea = React.memo(function Textarea({
  label,
  error,
  hint,
  id,
  className = '',
  required,
  rows = 4,
  ...props
}: TextareaProps) {
  const textareaId = id || `textarea-${Math.random().toString(36).slice(2, 9)}`
  const errorId = error ? `${textareaId}-error` : undefined
  const hintId = hint ? `${textareaId}-hint` : undefined

  return (
    <div className={`field ${error ? 'field--error' : ''} ${className}`.trim()}>
      {label && (
        <label htmlFor={textareaId} className="field__label">
          {label}
          {required && <span className="field__required" aria-hidden="true"> *</span>}
        </label>
      )}
      <textarea
        id={textareaId}
        className="field__input field__textarea"
        rows={rows}
        aria-invalid={!!error}
        aria-describedby={[errorId, hintId].filter(Boolean).join(' ') || undefined}
        required={required}
        {...props}
      />
      {hint && !error && <span id={hintId} className="field__hint">{hint}</span>}
      {error && <span id={errorId} className="field__error" role="alert">{error}</span>}

      <style>{`
        .field__textarea { resize: vertical; min-height: 80px; }
      `}</style>
    </div>
  )
})

export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  label?: string
  error?: string
  hint?: string
  options: { value: string; label: string; disabled?: boolean }[]
  placeholder?: string
}

export const Select = React.memo(function Select({
  label,
  error,
  hint,
  options,
  placeholder,
  id,
  className = '',
  required,
  ...props
}: SelectProps) {
  const selectId = id || `select-${Math.random().toString(36).slice(2, 9)}`
  const errorId = error ? `${selectId}-error` : undefined

  return (
    <div className={`field ${error ? 'field--error' : ''} ${className}`.trim()}>
      {label && (
        <label htmlFor={selectId} className="field__label">
          {label}
          {required && <span className="field__required" aria-hidden="true"> *</span>}
        </label>
      )}
      <div className="field__wrap field__select-wrap">
        <select
          id={selectId}
          className="field__input field__select"
          aria-invalid={!!error}
          aria-describedby={errorId}
          required={required}
          {...props}
        >
          {placeholder && <option value="">{placeholder}</option>}
          {options.map(opt => (
            <option key={opt.value} value={opt.value} disabled={opt.disabled}>
              {opt.label}
            </option>
          ))}
        </select>
        <span className="field__select-arrow" aria-hidden="true">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </span>
      </div>
      {hint && !error && <span className="field__hint">{hint}</span>}
      {error && <span id={errorId} className="field__error" role="alert">{error}</span>}

      <style>{`
        .field__select-wrap { position: relative; }
        .field__select {
          appearance: none;
          padding-right: 36px;
          cursor: pointer;
        }
        .field__select-arrow {
          position: absolute;
          right: 12px;
          top: 50%;
          transform: translateY(-50%);
          pointer-events: none;
          color: var(--color-text-muted, #64748b);
        }
      `}</style>
    </div>
  )
})
