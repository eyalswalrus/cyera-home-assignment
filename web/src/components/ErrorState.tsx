import { Alert, Button, Center } from '@mantine/core'
import { IconAlertTriangle } from '@tabler/icons-react'

export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <Center p="xl">
      <Alert color="red" icon={<IconAlertTriangle />} title="Something went wrong" maw={480}>
        {error.message}
        {onRetry && (
          <Button variant="light" color="red" size="xs" mt="sm" display="block" onClick={onRetry}>
            Try again
          </Button>
        )}
      </Alert>
    </Center>
  )
}
