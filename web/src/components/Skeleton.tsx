export default function Skeleton({ lines = 4 }: { lines?: number }) {
  return (
    <div className="skeleton-stack" aria-hidden="true">
      {Array.from({ length: lines }).map((_, index) => (
        <div key={index} className="skeleton" />
      ))}
    </div>
  );
}
