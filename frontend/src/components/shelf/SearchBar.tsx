import { SearchIcon } from "../common/icons";

interface SearchBarProps {
  value: string;
  onChange: (value: string) => void;
}

export function SearchBar({ value, onChange }: SearchBarProps) {
  return (
    <div className="glass relative flex items-center rounded-full px-4">
      <SearchIcon className="pointer-events-none h-4 w-4 text-kinora-muted" />
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Search your library…"
        aria-label="Search your library"
        className="w-full bg-transparent px-3 py-2.5 text-sm text-kinora-mist outline-none placeholder:text-kinora-muted/70"
      />
    </div>
  );
}
