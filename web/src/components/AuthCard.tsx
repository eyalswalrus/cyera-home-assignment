import { Center, Group, Paper, Stack, Text, Title } from '@mantine/core'
import { IconShieldLock } from '@tabler/icons-react'
import type { ReactNode } from 'react'

export function AuthCard({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  return (
    <Center mih="100vh" p="md" bg="var(--mantine-color-body)">
      <Stack w="100%" maw={400} gap="lg">
        <Group gap={8} justify="center">
          <IconShieldLock size={28} color="var(--mantine-primary-color-filled)" />
          <Text fw={700} size="xl">
            IdentityHub
          </Text>
        </Group>
        <Paper withBorder shadow="sm" p="xl">
          <Title order={2} size="h3">
            {title}
          </Title>
          <Text c="dimmed" size="sm" mb="lg">
            {subtitle}
          </Text>
          {children}
        </Paper>
      </Stack>
    </Center>
  )
}
