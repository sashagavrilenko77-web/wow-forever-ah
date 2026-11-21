-- AukcionExport: remembers what each character owns so the scripts in Desktop\Аукцион can read it.
-- Data reaches the disk when you log out or type /reload. Read-only: it never clicks or buys anything.

AukcionExportDB = AukcionExportDB or { chars = {} }

local frame = CreateFrame("Frame")
local charKey

local function now()
    return time()
end

local function char()
    if not charKey then
        charKey = (UnitName("player") or "?") .. "-" .. (GetRealmName() or "?")
    end
    AukcionExportDB.chars = AukcionExportDB.chars or {}
    local c = AukcionExportDB.chars[charKey] or {}
    AukcionExportDB.chars[charKey] = c
    return c
end

-- Container API differs between game versions.
local function numSlots(bag)
    if C_Container and C_Container.GetContainerNumSlots then
        return C_Container.GetContainerNumSlots(bag) or 0
    end
    return GetContainerNumSlots and GetContainerNumSlots(bag) or 0
end

local function slotItem(bag, slot)
    if C_Container and C_Container.GetContainerItemInfo then
        local info = C_Container.GetContainerItemInfo(bag, slot)
        if info then
            return info.itemID, info.stackCount
        end
        return nil
    end
    local _, count, _, _, _, _, _, _, _, itemID = GetContainerItemInfo(bag, slot)
    return itemID, count
end

local function scanBags(bags)
    local items = {}
    for _, bag in ipairs(bags) do
        for slot = 1, numSlots(bag) do
            local id, count = slotItem(bag, slot)
            if id then
                items[id] = (items[id] or 0) + (count or 1)
            end
        end
    end
    return items
end

local BAGS = { 0, 1, 2, 3, 4 }
local BANK = { -1, 5, 6, 7, 8, 9, 10 }

local function saveMoney()
    local c = char()
    c.gold = GetMoney()
    c.faction = UnitFactionGroup("player")
    c.level = UnitLevel("player")
    c.updated = now()
end

local function saveBags()
    local c = char()
    c.bags = scanBags(BAGS)
    c.bagsUpdated = now()
end

local function saveBank()
    local c = char()
    c.bank = scanBags(BANK)
    c.bankUpdated = now()
end

local function saveMail()
    if not GetInboxNumItems then
        return
    end
    local c = char()
    local items, money = {}, 0
    for i = 1, GetInboxNumItems() do
        local _, _, _, _, m = GetInboxHeaderInfo(i)
        money = money + (m or 0)
        for a = 1, (ATTACHMENTS_MAX_RECEIVE or 12) do
            local _, itemID, _, count = GetInboxItem(i, a)
            if itemID then
                items[itemID] = (items[itemID] or 0) + (count or 1)
            end
        end
    end
    c.mail, c.mailMoney, c.mailUpdated = items, money, now()
end

local function saveAuctions()
    if not (C_AuctionHouse and C_AuctionHouse.GetNumOwnedAuctions) then
        return
    end
    local c = char()
    local list = {}
    for i = 1, C_AuctionHouse.GetNumOwnedAuctions() do
        local a = C_AuctionHouse.GetOwnedAuctionInfo(i)
        if a and a.itemKey then
            list[#list + 1] = {
                item = a.itemKey.itemID,
                qty = a.quantity,
                buyout = a.buyoutAmount,
                bid = a.bidAmount,
                sold = (a.status == 1) and true or nil,
                timeLeft = a.timeLeftSeconds,
            }
        end
    end
    c.auctions, c.auctionsUpdated = list, now()
end

frame:RegisterEvent("PLAYER_LOGIN")
frame:RegisterEvent("PLAYER_MONEY")
frame:RegisterEvent("BAG_UPDATE_DELAYED")
frame:RegisterEvent("BANKFRAME_OPENED")
frame:RegisterEvent("PLAYERBANKSLOTS_CHANGED")
frame:RegisterEvent("MAIL_INBOX_UPDATE")
frame:RegisterEvent("PLAYER_LOGOUT")
pcall(frame.RegisterEvent, frame, "OWNED_AUCTIONS_UPDATED")
pcall(frame.RegisterEvent, frame, "AUCTION_HOUSE_SHOW")

local bankOpen = false
frame:RegisterEvent("BANKFRAME_CLOSED")

frame:SetScript("OnEvent", function(_, event)
    if event == "PLAYER_LOGIN" then
        saveMoney()
        saveBags()
    elseif event == "PLAYER_MONEY" then
        saveMoney()
    elseif event == "BAG_UPDATE_DELAYED" then
        saveBags()
        if bankOpen then
            saveBank()
        end
    elseif event == "BANKFRAME_OPENED" then
        bankOpen = true
        saveBank()
    elseif event == "BANKFRAME_CLOSED" then
        bankOpen = false
    elseif event == "PLAYERBANKSLOTS_CHANGED" then
        saveBank()
    elseif event == "MAIL_INBOX_UPDATE" then
        saveMail()
    elseif event == "AUCTION_HOUSE_SHOW" then
        if C_AuctionHouse and C_AuctionHouse.QueryOwnedAuctions then
            pcall(C_AuctionHouse.QueryOwnedAuctions, {})
        end
    elseif event == "OWNED_AUCTIONS_UPDATED" then
        saveAuctions()
    elseif event == "PLAYER_LOGOUT" then
        saveMoney()
        saveBags()
    end
end)
