// Models/ChapterComparison.cs
namespace PdfCompareApp.Models
{
    public class ChapterComparison
    {
        public int Id { get; set; }
        public string ChapterName { get; set; } = string.Empty;
        public string Status { get; set; } = "unchanged";
        // "changed" | "added" | "removed" | "unchanged"

        public PageRange? OriginalPages { get; set; }
        public PageRange? ProcessedPages { get; set; }

        public double SimilarityScore { get; set; } = 100.0;
        public string Summary { get; set; } = string.Empty;
    }

    public class PageRange
    {
        public int Start { get; set; }
        public int End { get; set; }

        public bool ContainsPage(int pageNum) =>
            pageNum >= Start && pageNum <= End;
    }
}