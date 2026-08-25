import { toast } from "sonner"

type NotificationCopy = {
  title: string
  description?: string
}

export const notifications = {
  success({ title, description }: NotificationCopy) {
    return toast.success(title, { description })
  },
  info({ title, description }: NotificationCopy) {
    return toast.info(title, { description })
  },
  error({ title, description }: NotificationCopy) {
    return toast.error(title, { description, duration: 6000 })
  },
}
