import { continueReading, recentlyAdded, popularOnKinora, recommended } from "../data/books";
import BookCard from "./BookCard";

export default function FavoritesPage() {
  // Simulate favorites — books with progress or all from recently added
  const favorites = [...continueReading.filter((b) => b.progress > 0), ...recentlyAdded.slice(0, 3)];

  return (
    <div className="pt-12 pb-8 px-6 max-w-[1280px] mx-auto relative z-10">
      <h1 className="font-serif text-2xl font-semibold text-kinora-text mb-2 pt-4">
        Favorites
      </h1>
      <p className="text-sm text-kinora-muted mb-8">
        {favorites.length} books you love
      </p>

      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-4">
        {favorites.map((book) => (
          <BookCard key={book.id} book={book} />
        ))}
      </div>
    </div>
  );
}
