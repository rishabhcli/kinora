import type { Book } from "../data/books";
import {
  continueReading,
  recentlyAdded,
  popularOnKinora,
  recommended,
} from "../data/books";
import BookShelf from "./BookShelf";

export default function LibraryPage() {
  const allBooks: Book[] = [
    ...continueReading,
    ...recentlyAdded,
    ...popularOnKinora,
    ...recommended,
  ];

  return (
    <div className="pt-12 pb-8 px-6 max-w-[1280px] mx-auto relative z-10">
      <h1 className="font-serif text-2xl font-semibold text-kinora-text mb-6 pt-4">
        My Library
      </h1>
      <p className="text-sm text-kinora-muted mb-8">
        {allBooks.length} books in your collection
      </p>
      <BookShelf title="Continue Reading" books={continueReading} />
      <BookShelf title="Recently Added" books={recentlyAdded} />
      <BookShelf title="Popular on Kinora" books={popularOnKinora} />
      <BookShelf title="Recommended for You" books={recommended} />
    </div>
  );
}
