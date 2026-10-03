import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        print("=== Step 2: Login with wrong password ===")
        await page.goto('http://localhost:3000/login')
        await page.fill('input[type="email"]', 'test_subagent@company.com')
        await page.fill('input[type="password"]', 'wrongpass')
        await page.click('button[type="submit"]')
        
        await asyncio.sleep(1)
        
        print("URL after bad login:", page.url)
        email_val = await page.input_value('input[type="email"]')
        password_val = await page.input_value('input[type="password"]')
        print("Email field retained:", email_val == 'test_subagent@company.com')
        print("Password field retained:", password_val == 'wrongpass')
        
        try:
            error_text = await page.text_content('.bg-rose-50', timeout=2000)
            print("Error text displayed:", error_text.strip().replace('\n', ' '))
        except:
            print("Error text displayed: None")
            
        print("\n=== Step 3: Login with correct password ===")
        await page.goto('http://localhost:3000/signup')
        await page.fill('input[type="text"]', 'TestCorp')
        await page.fill('input[type="email"]', 'test_subagent@company.com')
        await page.fill('input[type="password"]', 'password123')
        await page.click('button[type="submit"]')
        await asyncio.sleep(1.5)
        
        print("URL after first signup:", page.url)
        
        await page.goto('http://localhost:3000/login')
        await page.fill('input[type="email"]', 'test_subagent@company.com')
        await page.fill('input[type="password"]', 'password123')
        await page.click('button[type="submit"]')
        await asyncio.sleep(1.5)
        
        print("URL after correct login:", page.url)
        
        print("\n=== Step 4: Signup duplicate ===")
        await page.goto('http://localhost:3000/signup')
        await page.fill('input[type="text"]', 'TestCorp')
        await page.fill('input[type="email"]', 'test_subagent@company.com')
        await page.fill('input[type="password"]', 'password123')
        await page.click('button[type="submit"]')
        await asyncio.sleep(1)
        
        try:
            error_text_signup = await page.text_content('.bg-rose-50', timeout=2000)
            print("Signup Error text displayed:", error_text_signup.strip().replace('\n', ' '))
        except:
            print("Signup Error text displayed: None")
            
        print("\n=== Step 5: Unauthenticated access ===")
        await page.goto('http://localhost:3000/dashboard')
        await page.evaluate('localStorage.clear()')
        await page.reload()
        await asyncio.sleep(1)
        print("URL after clear storage and reload /dashboard:", page.url)

        await browser.close()

asyncio.run(main())
