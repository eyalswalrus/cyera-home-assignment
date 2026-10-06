import { MultiSelect } from '@mantine/core'
import { useState } from 'react'
import { type Project, type ProjectSource, useProjectOptions } from '../api/hooks'

const projectLabel = (p: Project) => `${p.name} (${p.key})`

/** Searchable multi-select of Jira projects (searched in Jira). `source` decides which projects are
 * offered: ones the user can create issues in, or ones both the user and the digest bot can. */
export function ProjectMultiSelect({
  value,
  onChange,
  label,
  description,
  error,
  source = 'jira',
  required = false,
  maxValues = 50,
  knownProjects = [],
}: {
  value: string[]
  onChange: (keys: string[]) => void
  label: string
  description?: string
  error?: string
  source?: ProjectSource
  required?: boolean
  maxValues?: number
  /** Projects already chosen elsewhere (e.g. saved subscriptions), so their names show at once. */
  knownProjects?: { key: string; name: string }[]
}) {
  const [search, setSearch] = useState('')
  const projects = useProjectOptions(search, true, source)
  // Labels of chosen projects, captured when picked, so they survive later searches.
  const [chosenLabels, setChosenLabels] = useState<Record<string, string>>({})

  const options = new Map<string, string>()
  for (const p of knownProjects) options.set(p.key, `${p.name} (${p.key})`)
  for (const key of value) options.set(key, chosenLabels[key] ?? options.get(key) ?? key)
  for (const p of projects.projects) options.set(p.key, projectLabel(p))

  const change = (keys: string[]) => {
    setChosenLabels((prev) => {
      const next = { ...prev }
      for (const key of keys) next[key] ??= options.get(key) ?? key
      return next
    })
    onChange(keys)
  }

  return (
    <MultiSelect
      label={label}
      description={description}
      placeholder={value.length ? undefined : 'Search by name or key'}
      withAsterisk={required}
      searchable
      searchValue={search}
      onSearchChange={setSearch}
      data={[...options].map(([key, text]) => ({ value: key, label: text }))}
      value={value}
      onChange={change}
      // Typing narrows the loaded options instantly (Jira search fills in the rest) and highlights
      // the first match, so Enter picks it.
      selectFirstOptionOnChange
      onKeyDown={(event) => {
        // Enter in the picker selects a project; it must never submit a surrounding form.
        if (event.key === 'Enter') event.preventDefault()
      }}
      nothingFoundMessage={projects.isFetching ? 'Searching…' : 'No matching projects'}
      error={error ?? projects.error?.message}
      maxValues={maxValues}
      hidePickedOptions
    />
  )
}
