using PdfComparisonApp.Models;
using System.Text.Json;

namespace PdfComparisonApp.Services
{
    public interface IPdfApiService
    {
        Task<ProcessPdfApiResponse> ProcessPdfsAsync(UploadPdfViewModel model);
        Task<CompareAllApiResponse> CompareAllChaptersAsync(int pdfIdV1, int pdfIdV2);
        Task<ChapterComparisonDto> CompareSingleChapterAsync(int pdfIdV1, int pdfIdV2, int chapterNumber);
        Task<JsonElement> GetHistoryAsync();
        Task<JsonElement> GetComparisonDetailAsync(int pdfIdV1, int pdfIdV2);
    }
}