import { Loader, Select } from '@mantine/core'
import { useDebouncedValue } from '@mantine/hooks'
import { useState } from 'react'
import { type Project, useProjects } from '../api/hooks'

const optionLabel = (p: Project) => `${p.name} (${p.key})`

/** Searchable project picker. Filtering happens in Jira (by name or key), so it scales to sites
 * with hundreds of projects, and only projects the user can create issues in are offered. */
export function ProjectPicker({ value, onChange }: { value: Project | null; onChange: (p: Project | null) => void }) {
  const [search, setSearch] = useState('')
  const [opened, setOpened] = useState(false)
  // Once a project is picked, the input shows its label; don't send that label as a search.
  const query = value && search === optionLabel(value) ? '' : search.trim()
  const [debouncedQuery] = useDebouncedValue(query, 250)
  const projects = useProjects(debouncedQuery, opened || !value)

  // Keep the selected project in the options so the input can display it.
  const options = [...(projects.data ?? [])]
  if (value && !options.some((p) => p.key === value.key)) options.unshift(value)

  const searching = projects.isFetching || debouncedQuery !== query
  return (
    <Select
      label="Jira project"
      description="Only projects where you can create issues are listed."
      placeholder="Search by name or key"
      searchable
      searchValue={search}
      onSearchChange={setSearch}
      data={options.map((p) => ({ value: p.key, label: optionLabel(p) }))}
      value={value?.key ?? null}
      onChange={(key) => onChange(options.find((p) => p.key === key) ?? null)}
      filter={({ options: items }) => items} // already filtered by Jira
      onDropdownOpen={() => setOpened(true)}
      onDropdownClose={() => setOpened(false)}
      nothingFoundMessage={
        searching ? 'Searching…' : query ? `No projects match "${query}"` : 'No projects you can create issues in'
      }
      rightSection={searching && opened ? <Loader size="xs" /> : undefined}
      error={projects.error?.message}
      allowDeselect={false}
    />
  )
}
