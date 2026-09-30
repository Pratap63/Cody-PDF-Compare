using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using PdfComparisonApp.Models;

namespace PdfComparisonApp.Services
{
    public class PdfApiService : IPdfApiService
    {
        private readonly HttpClient _httpClient;
        private readonly JsonSerializerOptions _jsonOptions;

        public PdfApiService(HttpClient httpClient, IConfiguration configuration)
        {
            _httpClient = httpClient;
            var baseUrl = configuration["PythonApiSettings:BaseUrl"] ?? "http://localhost:8000";
            _httpClient.BaseAddress = new Uri(baseUrl);

            // Timeout increase karo kyunki LLM comparisons time le sakte hain
            _httpClient.Timeout = TimeSpan.FromMinutes(5);

            _jsonOptions = new JsonSerializerOptions { PropertyNameCaseInsensitive = true };
        }

        public async Task<ProcessPdfApiResponse> ProcessPdfsAsync(UploadPdfViewModel model)
        {
            using var formData = new MultipartFormDataContent();

            // PDF 1
            var fileContent1 = new StreamContent(model.PdfV1.OpenReadStream());
            fileContent1.Headers.ContentType = new MediaTypeHeaderValue("application/pdf");
            formData.Add(fileContent1, "pdf_v1", model.PdfV1.FileName);

            // PDF 2
            var fileContent2 = new StreamContent(model.PdfV2.OpenReadStream());
            fileContent2.Headers.ContentType = new MediaTypeHeaderValue("application/pdf");
            formData.Add(fileContent2, "pdf_v2", model.PdfV2.FileName);

            // Form metadata
            formData.Add(new StringContent(model.DocName ?? "EOC"), "doc_name");
            formData.Add(new StringContent(model.VersionV1 ?? "2027"), "version_v1");
            formData.Add(new StringContent(model.VersionV2 ?? "2028"), "version_v2");

            var response = await _httpClient.PostAsync("/api/pdf/process", formData);
            response.EnsureSuccessStatusCode();

            var responseJson = await response.Content.ReadAsStringAsync();
            return JsonSerializer.Deserialize<ProcessPdfApiResponse>(responseJson, _jsonOptions);
        }

        public async Task<CompareAllApiResponse> CompareAllChaptersAsync(int pdfIdV1, int pdfIdV2)
        {
            var payload = new { pdf_id_v1 = pdfIdV1, pdf_id_v2 = pdfIdV2 };
            var content = new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json");

            var response = await _httpClient.PostAsync("/api/pdf/compare-all", content);
            response.EnsureSuccessStatusCode();

            var responseJson = await response.Content.ReadAsStringAsync();
            Console.WriteLine("COMPARE ALL PYTHON RESPONSE:");
            Console.WriteLine(responseJson);

            return JsonSerializer.Deserialize<CompareAllApiResponse>(responseJson, _jsonOptions);
        }

        public async Task<ChapterComparisonDto> CompareSingleChapterAsync(int pdfIdV1, int pdfIdV2, int chapterNumber)
        {
            var payload = new { pdf_id_v1 = pdfIdV1, pdf_id_v2 = pdfIdV2, chapter_number = chapterNumber };
            var content = new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json");

            var response = await _httpClient.PostAsync("/api/pdf/compare-chapter", content);
            response.EnsureSuccessStatusCode();

            var responseJson = await response.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(responseJson);
            var dataObj = doc.RootElement.GetProperty("data").GetRawText();

            return JsonSerializer.Deserialize<ChapterComparisonDto>(dataObj, _jsonOptions);
        }

        public async Task<JsonElement> GetHistoryAsync()
        {
            var response = await _httpClient.GetAsync("/api/pdf/history");
            response.EnsureSuccessStatusCode();
            var json = await response.Content.ReadAsStringAsync();
            return JsonDocument.Parse(json).RootElement;
        }

        public async Task<JsonElement> GetComparisonDetailAsync(int pdfIdV1, int pdfIdV2)
        {
            var response = await _httpClient.GetAsync($"/api/pdf/history/{pdfIdV1}/{pdfIdV2}");
            response.EnsureSuccessStatusCode();
            var json = await response.Content.ReadAsStringAsync();
            return JsonDocument.Parse(json).RootElement;
        }
    }
}