$url = "http://172.17.3.135:8000/v1/chat/completions"
 
$body = @{
    model = "Qwen/Qwen3.6-35B-A3B"
    messages = @(
        @{
            role = "user"
            content = "hi"
        }
    )
    max_tokens = 100
    chat_template_kwargs = @{
        enable_thinking = $true
    }
} | ConvertTo-Json -Depth 5
 
$response = Invoke-RestMethod `
    -Uri $url `
    -Method POST `
    -ContentType "application/json" `
    -Body $body
 
# 원시 응답 전체 출력
Write-Host "=== 원시 응답 ===" -ForegroundColor Cyan
$response | ConvertTo-Json -Depth 10
 
# 핵심 필드만 추출
Write-Host "`n=== 파싱 결과 ===" -ForegroundColor Cyan
$message = $response.choices[0].message
 
Write-Host "content: $($message.content)" -ForegroundColor Yellow
Write-Host "reasoning_content: $($message.reasoning_content)" -ForegroundColor Green