import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { searchAddresses, sourceLink, type AddressSearchResponse, type AddressSearchResult } from '../lib/api'

type SearchState = {
  status: 'idle' | 'loading' | 'ready' | 'error'
  query: string
  data: AddressSearchResponse | null
  error: string | null
}

type Props = { onSelect: (result: AddressSearchResult) => void }

function preferredResult(data: AddressSearchResponse) {
  if (typeof data.best_match_id === 'number') return data.results.find((result) => result.id === data.best_match_id)
  return undefined
}

function selectionHint(data: AddressSearchResponse) {
  if (data.status === 'empty') return 'Warsaw address data is not available yet.'
  if (!data.results.length) return 'No matching Warsaw address was found.'
  if ((data.exact_match_count ?? 0) > 1) return 'Several addresses match. Choose the correct address and postcode below.'
  return 'Choose an address below to select the right building.'
}

export function AddressSearch({ onSelect }: Props) {
  const inputId = useId()
  const listId = `${inputId}-results`
  const descriptionId = `${inputId}-description`
  const inputRef = useRef<HTMLInputElement>(null)
  const generation = useRef(0)
  const activeRequest = useRef<AbortController | null>(null)
  const acceptedQuery = useRef<string | null>(null)
  const acceptedResult = useRef<AddressSearchResult | null>(null)
  const pendingSubmit = useRef<string | null>(null)
  const [query, setQuery] = useState('')
  const [state, setState] = useState<SearchState>({ status: 'idle', query: '', data: null, error: null })
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const [revision, setRevision] = useState(0)
  const [notice, setNotice] = useState<string | null>(null)

  const chooseResult = useCallback((result: AddressSearchResult) => {
    generation.current += 1
    activeRequest.current?.abort()
    pendingSubmit.current = null
    acceptedQuery.current = result.address
    acceptedResult.current = result
    setQuery(result.address)
    setState((previous) => ({
      status: 'ready',
      query: result.address,
      data: { query: result.address, results: [result], source: previous.data?.source ?? null, status: 'ready', best_match_id: result.id },
      error: null,
    }))
    setOpen(false)
    setActiveIndex(-1)
    setNotice(result.match_status === 'matched'
      ? `Selected ${result.address}`
      : result.match_status === 'ambiguous'
        ? `Address found. Select the correct building on the map.`
        : `Address found. Its exact building is unconfirmed.`)
    onSelect(result)
  }, [onSelect])

  useEffect(() => {
    const trimmed = query.trim()
    if (trimmed.length < 2 || acceptedQuery.current === query) return
    const requestGeneration = ++generation.current
    const controller = new AbortController()
    activeRequest.current = controller
    let timeout: number | undefined
    let disposed = false

    const debounce = window.setTimeout(() => {
      timeout = window.setTimeout(() => controller.abort(), 12000)
      searchAddresses(trimmed, controller.signal).then((data) => {
        if (disposed || generation.current !== requestGeneration) return
        setState({ status: 'ready', query: trimmed, data, error: null })
        setActiveIndex(-1)
        if (pendingSubmit.current === trimmed) {
          pendingSubmit.current = null
          const preferred = preferredResult(data)
          if (preferred) chooseResult(preferred)
          else {
            setOpen(true)
            setNotice(selectionHint(data))
          }
        }
      }).catch(() => {
        if (disposed || generation.current !== requestGeneration) return
        pendingSubmit.current = null
        setState({ status: 'error', query: trimmed, data: null, error: controller.signal.aborted ? 'Address search took too long. Please try again.' : 'Address search is unavailable. Please try again.' })
      }).finally(() => window.clearTimeout(timeout))
    }, pendingSubmit.current === trimmed ? 0 : 250)

    return () => {
      disposed = true
      window.clearTimeout(debounce)
      window.clearTimeout(timeout)
      controller.abort()
      if (activeRequest.current === controller) activeRequest.current = null
    }
  }, [query, revision, chooseResult])

  useEffect(() => {
    if (open && activeIndex >= 0) document.getElementById(`${listId}-${activeIndex}`)?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex, listId, open])

  function changeQuery(value: string) {
    generation.current += 1
    activeRequest.current?.abort()
    acceptedQuery.current = null
    acceptedResult.current = null
    pendingSubmit.current = null
    setQuery(value)
    setNotice(null)
    setActiveIndex(-1)
    setOpen(value.trim().length >= 2)
    setState({ status: value.trim().length >= 2 ? 'loading' : 'idle', query: value.trim(), data: null, error: null })
  }

  function submitSearch() {
    const trimmed = query.trim()
    if (trimmed.length < 2) {
      setNotice('Enter at least two characters of a Warsaw address.')
      inputRef.current?.focus()
      return
    }
    if (acceptedQuery.current === query && acceptedResult.current && activeIndex < 0) {
      chooseResult(acceptedResult.current)
      return
    }
    if (state.status === 'ready' && state.query === trimmed && state.data) {
      const result = activeIndex >= 0 ? state.data.results[activeIndex] : preferredResult(state.data)
      if (result) chooseResult(result)
      else {
        setOpen(true)
        setNotice(selectionHint(state.data))
      }
      return
    }
    acceptedQuery.current = null
    pendingSubmit.current = trimmed
    setOpen(true)
    setNotice(null)
    setState({ status: 'loading', query: trimmed, data: null, error: null })
    setRevision((value) => value + 1)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.nativeEvent.isComposing) return
    const results = state.query === query.trim() ? state.data?.results ?? [] : []
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      setOpen(true)
      if (results.length) setActiveIndex((current) => event.key === 'ArrowDown' ? (current + 1) % results.length : current <= 0 ? results.length - 1 : current - 1)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      pendingSubmit.current = null
      setOpen(false)
      setActiveIndex(-1)
    }
  }

  const results = state.data?.results ?? []
  const showPopup = open && query.trim().length >= 2
  const source = state.data?.source
  const sourceHref = source ? sourceLink(source.url) : undefined

  return (
    <div className="address-search" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false)
    }}>
      <form className="address-search-form" role="search" aria-label="Find a Warsaw building by address" onSubmit={(event) => { event.preventDefault(); submitSearch() }}>
        <label htmlFor={inputId}>Search an address</label>
        <div className="address-input-row">
          <div className="address-input-wrap">
            <svg className="search-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m15.5 15.5 4.5 4.5" /></svg>
            <input ref={inputRef} id={inputId} type="text" role="combobox" autoComplete="off" spellCheck={false} maxLength={120} placeholder="Street and number, e.g. Złota 59" value={query} onChange={(event) => changeQuery(event.target.value)} onFocus={() => query.trim().length >= 2 && setOpen(true)} onKeyDown={handleKeyDown} aria-autocomplete="list" aria-expanded={showPopup} aria-controls={listId} aria-activedescendant={showPopup && activeIndex >= 0 ? `${listId}-${activeIndex}` : undefined} aria-describedby={descriptionId} />
            {query && <button type="button" className="clear-search" aria-label="Clear address search" onClick={() => { changeQuery(''); inputRef.current?.focus() }}>×</button>}
          </div>
          <button type="submit" className="address-submit">Search<span aria-hidden="true">↗</span></button>
        </div>
      </form>
      <p id={descriptionId} className={`search-description ${notice ? 'search-description-notice' : ''}`} role="status">{notice ?? 'Search Warsaw or choose a building on the map.'}</p>
      {showPopup && <div className="address-popup">
        {state.status === 'loading' && <p className="search-feedback" role="status"><span className="search-loading-dot" />Searching Warsaw addresses…</p>}
        {state.status === 'error' && <div className="search-feedback search-feedback-error" role="alert"><p>{state.error}</p><button type="button" className="text-button" onClick={submitSearch}>Try again ↻</button></div>}
        {state.status === 'ready' && !results.length && <p className="search-feedback" role="status">{state.data?.status === 'empty' ? 'Address search is temporarily unavailable.' : 'No matching address. Check the street name and building number.'}</p>}
        <ul id={listId} role="listbox" aria-label="Warsaw address suggestions" aria-busy={state.status === 'loading'}>
          {results.map((result, index) => <li id={`${listId}-${index}`} key={result.id} role="option" aria-selected={index === activeIndex} className={index === activeIndex ? 'address-option address-option-active' : 'address-option'} onMouseDown={(event) => event.preventDefault()} onClick={() => chooseResult(result)}><span className="address-option-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M19 10c0 5-7 10-7 10S5 15 5 10a7 7 0 1 1 14 0Z" /><circle cx="12" cy="10" r="2.5" /></svg></span><span><strong>{result.address}</strong><small>{result.match_status === 'matched' ? 'View building' : result.match_status === 'ambiguous' ? 'Choose the building on the map' : 'Exact building unconfirmed'}</small></span><span className="address-option-arrow" aria-hidden="true">↗</span></li>)}
        </ul>
        {source && <div className="search-source">Addresses: {sourceHref ? <a href={sourceHref} target="_blank" rel="noreferrer">{source.attribution || source.name}</a> : source.attribution || source.name}</div>}
      </div>}
    </div>
  )
}
