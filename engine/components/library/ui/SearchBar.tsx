'use client'

import React, { useState, useRef, useCallback } from 'react'

export interface SearchBarProps {
  value?: string
  defaultValue?: string
  onChange?: (value: string) => void
  onSearch?: (value: string) => void
  placeholder?: string
  loading?: boolean
  clearable?: boolean
  className?: string
  size?: 'sm' | 'md' | 'lg'
  debounce?: number
}

export const SearchBar = React.memo(function SearchBar({
  value: controlledValue,
  defaultValue = '',
  onChange,
  onSearch,
  placeholder = '검색...',
  loading = false,
  clearable = true,
  className = '',
  size = 'md',
  debounce: debounceMs = 0,
}: SearchBarProps) {
  const [internalValue, setInternalValue] = useState(defaultValue)
  const value = controlledValue !== undefined ? controlledValue : internalValue
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const handleChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.value
    if (controlledValue === undefined) setInternalValue(v)
    if (debounceMs > 0) {
      if (timerRef.current) clearTimeout(timerRef.current)
      timerRef.current = setTimeout(() => onChange?.(v), debounceMs)
    } else {
      onChange?.(v)
    }
  }, [controlledValue, debounceMs, onChange])

  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') onSearch?.(value)
    if (e.key === 'Escape') {
      if (controlledValue === undefined) setInternalValue('')
      onChange?.('')
    }
  }, [value, onSearch, onChange, controlledValue])

  const handleClear = useCallback(() => {
    if (controlledValue === undefined) setInternalValue('')
    onChange?.('')
    onSearch?.('')
  }, [controlledValue, onChange, onSearch])

  return (
    <div className={`searchbar searchbar--${size} ${className}`.trim()} role="search">
      <span className="searchbar__icon" aria-hidden="true">
        {loading ? (
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="2" strokeDasharray="25 13" className="searchbar__spin"/>
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor" strokeWidth="1.5"/>
            <path d="M10 10l3.5 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
          </svg>
        )}
      </span>
      <input
        type="search"
        className="searchbar__input"
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        aria-label={placeholder}
        autoComplete="off"
        spellCheck={false}
      />
      {clearable && value && (
        <button
          className="searchbar__clear"
          onClick={handleClear}
          type="button"
          aria-label="검색어 지우기"
          tabIndex={0}
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
            <path d="M1 1l12 12M13 1L1 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
          </svg>
        </button>
      )}

      <style>{`
        .searchbar { position: relative; display: flex; align-items: center; }
        .searchbar--sm { height: 34px; }
        .searchbar--md { height: 40px; }
        .searchbar--lg { height: 48px; }
        .searchbar__icon {
          position: absolute; left: 12px;
          display: flex; align-items: center;
          color: var(--color-text-muted, #64748b);
          pointer-events: none;
        }
        .searchbar__input {
          width: 100%; height: 100%;
          padding: 0 36px 0 38px;
          font-size: 0.9375rem; font-family: inherit;
          color: var(--color-text, #1a1a1a);
          background: var(--color-surface, #f8fafc);
          border: 1px solid var(--color-border, #e2e8f0);
          border-radius: var(--radius-md, 8px);
          outline: none;
          transition: border-color 0.15s, box-shadow 0.15s;
        }
        .searchbar__input::-webkit-search-cancel-button { display: none; }
        .searchbar__input:focus {
          border-color: var(--color-primary, #3b82f6);
          box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-primary, #3b82f6) 20%, transparent);
        }
        .searchbar__clear {
          position: absolute; right: 10px;
          display: flex; align-items: center; justify-content: center;
          width: 22px; height: 22px;
          background: var(--color-border, #e2e8f0);
          border: none; border-radius: 50%;
          cursor: pointer;
          color: var(--color-text-muted, #64748b);
          transition: background 0.15s;
        }
        .searchbar__clear:hover { background: var(--color-text-muted, #94a3b8); color: #fff; }
        .searchbar__clear:focus-visible { outline: 2px solid var(--color-primary, #3b82f6); }
        .searchbar__spin { animation: searchbar-spin 0.8s linear infinite; }
        @keyframes searchbar-spin { to { transform: rotate(360deg); transform-origin: 50% 50%; } }
        @media (prefers-reduced-motion: reduce) { .searchbar__spin { animation: none; } }
      `}</style>
    </div>
  )
})
