import {
  LayoutGrid,
  MessageSquare,
  Settings,
} from "lucide-react"

export function WorkspaceTopbar() {
  return (
    <div className="workspace-topbar">
      <div className="topbar-spacer" />
      <div className="topbar-actions">
        <button className="topbar-icon" aria-label="Layout"><LayoutGrid size={15} /></button>
        <button className="topbar-icon" aria-label="Messages"><MessageSquare size={15} /></button>
        <button className="topbar-icon" aria-label="Settings"><Settings size={15} /></button>
        <button className="topbar-icon" aria-label="Theme"><span className="theme-chip" /></button>
      </div>
    </div>
  )
}
